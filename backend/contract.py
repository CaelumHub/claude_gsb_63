"""Smart-contract engine: deploy, invoke, simulate, and the contract API.

A contract is a restricted-Python module (validated by :mod:`sandbox`) that
interacts with the chain only through the injected globals:

* ``state``   — a persistent dict-like key/value store (survives across calls);
* ``msg``     — ``.sender``, ``.value``, ``.address`` of the current call;
* ``emit``    — ``emit(name, **data)`` appends an event to the contract log;
* ``require`` — ``require(cond, msg)`` aborts the call if ``cond`` is false;
* ``transfer``— ``transfer(to, amount)`` sends the contract's balance outward;
* ``balance_of`` / ``this_balance`` — balance introspection helpers;
* ``deploy_contract`` — issue a new contract instance from inside a contract
  (used by factory contracts to mint child instances);
* ``this_factory`` — address of the factory that created this contract (or
  ``None`` for plain deployments).

Deployment runs the module (or an optional ``init(...)`` entry point); invoking
runs a named function.  Every mutation flows through the :class:`WorldState`,
so the state root in the block header covers contract storage as well.

Factory lifecycle
-----------------
A contract may act as a *factory*: its ``deploy_contract`` calls create child
instances that are tagged with the factory's address.  Before any instance is
invoked (or read-simulated), the engine asks the factory whether the instance
is active via an optional ``is_instance_active(address)`` view; a falsy result
halts the call.  Factories therefore fully own the running/disabled lifecycle
of everything they issue.
"""

import copy
import json

from . import crypto
from .sandbox import SandboxError, exec_restricted, call_function, validate_source
from .state import ZERO_ADDRESS

# Prefix that distinguishes factory-minted child addresses (0xf) from
# ordinary deployment addresses (0xc).
FACTORY_ADDR_PREFIX = "0xf"
# Maximum nesting depth of deploy_contract calls inside one outer invocation.
MAX_CONTRACT_DEPTH = 10


# --------------------------------------------------------------------------- #
# Value / storage guards
# --------------------------------------------------------------------------- #
def ensure_jsonable(value, depth=0):
    """Raise if ``value`` cannot be persisted as JSON; enforce nesting depth."""
    if depth > 8:
        raise SandboxError("state value nesting too deep")
    if value is None or isinstance(value, (bool, int, float, str)):
        # Reject non-finite floats (NaN / Inf break JSON and equality checks).
        if isinstance(value, float) and value != value:
            raise SandboxError("NaN is not a valid state value")
        return value
    if isinstance(value, (list, tuple)):
        return [ensure_jsonable(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): ensure_jsonable(v, depth + 1) for k, v in value.items()}
    raise SandboxError(f"value of type {type(value).__name__} cannot be stored")


class StateStore:
    """Dict-like facade over a contract's persistent storage.

    Values are validated to be JSON-serializable, and the key count is bounded.
    """

    def __init__(self, storage, max_keys):
        self._storage = storage
        self._max_keys = max_keys

    def __getitem__(self, key):
        return self._storage[key]

    def __setitem__(self, key, value):
        key = str(key)
        value = ensure_jsonable(value)
        if key not in self._storage and len(self._storage) >= self._max_keys:
            raise SandboxError("contract state key limit reached")
        self._storage[key] = value

    def __delitem__(self, key):
        del self._storage[key]

    def __contains__(self, key):
        return key in self._storage

    def __len__(self):
        return len(self._storage)

    def __iter__(self):
        return iter(self._storage)

    def get(self, key, default=None):
        return self._storage.get(key, default)

    def keys(self):
        return self._storage.keys()

    def values(self):
        return self._storage.values()

    def items(self):
        return self._storage.items()


class _Msg:
    def __init__(self, sender, value, address):
        self.sender = sender
        self.value = value
        self.address = address


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class ContractEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.max_keys = cfg.get("CONTRACT_MAX_STATE_KEYS", 2000)
        self.max_events = cfg.get("CONTRACT_MAX_EVENTS", 1000)
        self.max_print = cfg.get("SANDBOX_MAX_PRINT", 50_000)

    # -- context ----------------------------------------------------------- #
    def build_context(self, world_state, contract_addr, sender, value, height,
                      depth=0, factory_addr=None):
        contract = world_state.contract(contract_addr)
        storage = contract["storage"] if contract else {}
        store = StateStore(storage, self.max_keys)
        events = []
        transfers = []

        def emit(event, **data):
            event = str(event)
            data = ensure_jsonable(data)
            if len(events) >= self.max_events:
                raise SandboxError("contract event limit reached")
            events.append({"event": event, "data": data, "seq": len(events) + 1})

        def require(cond, message="require failed"):
            if not cond:
                raise SandboxError(f"require failed: {message}")

        def transfer(to, amount):
            amount = float(amount)
            if amount < 0:
                raise SandboxError("transfer amount must be non-negative")
            cur = world_state.balance(contract_addr)
            if amount > cur:
                raise SandboxError("contract has insufficient balance to transfer")
            world_state.add_balance(contract_addr, -amount)
            world_state.add_balance(to, amount)
            transfers.append({"to": to, "amount": amount})

        def balance_of(addr):
            return world_state.balance(addr)

        def this_balance():
            return world_state.balance(contract_addr)

        def deploy_contract(code, *args):
            """Issue a new contract instance from inside a contract.

            ``code`` is the child contract source; remaining positional
            arguments are passed to its ``init`` function.  The child address
            is deterministic from (factory address, factory-local nonce), the
            original caller (``msg.sender``) is recorded as the child's
            creator, and the child is tagged with the current contract as its
            factory.  Returns the child contract's address.
            """
            if depth + 1 >= MAX_CONTRACT_DEPTH:
                raise SandboxError("contract deployment depth limit reached")
            if not isinstance(code, str) or not code.strip():
                raise SandboxError("deploy_contract requires contract source")
            # Factory-local nonce → deterministic, collision-resistant address.
            nonce_key = "__child_nonce"
            nonce = int(store.get(nonce_key, 0)) + 1
            store[nonce_key] = nonce
            address = self.child_address(world_state, contract_addr, nonce)
            result = self.deploy(
                code, sender, address, world_state,
                constructor=list(args), height=height,
                depth=depth + 1, factory_addr=contract_addr)
            if not result["ok"]:
                raise SandboxError(result["error"] or "child deploy failed")
            # Surface child init events/transfers on the parent's receipt.
            events.extend(result["events"])
            transfers.extend(result["transfers"])
            emit("InstanceDeployed", child=address, by=sender)
            return address

        def this_factory():
            return factory_addr

        context = {
            "state": store,
            "msg": _Msg(sender, float(value), contract_addr),
            "emit": emit,
            "require": require,
            "transfer": transfer,
            "balance_of": balance_of,
            "this_balance": this_balance,
            "deploy_contract": deploy_contract,
            "this_factory": this_factory,
            "block_height": height,
        }
        return context, events, transfers

    def child_address(self, world_state, factory_addr, nonce):
        """Deterministic address for a factory-minted child instance."""
        base = FACTORY_ADDR_PREFIX + crypto.sha256(
            f"child:{factory_addr}:{nonce}".encode()).hex()[:39]
        address = base
        bump = 0
        while world_state.contract(address):
            # Deterministically walk forward if the slot is occupied.
            address = FACTORY_ADDR_PREFIX + crypto.sha256(
                f"child:{factory_addr}:{nonce}:{bump}".encode()).hex()[:39]
            bump += 1
        return address

    def factory_allows(self, world_state, factory_addr, child_addr, height):
        """Ask a child instance's factory whether the instance is active.

        Returns ``(allowed, reason)``.  ``factory_addr`` falsy means the
        contract was deployed directly (always allowed).  A factory contract
        that does not expose an ``is_instance_active`` view is treated as
        allowing everything (backwards compatible).
        """
        if not factory_addr:
            return True, None
        factory = world_state.contract(factory_addr)
        if not factory:
            # The factory that issued this instance is gone — fail closed.
            return False, f"factory {factory_addr} not found"
        ctx, _events, _transfers = self.build_context(
            world_state, factory_addr, ZERO_ADDRESS, 0, height)
        res = call_function(factory["code"], "is_instance_active",
                            [child_addr], ctx, output_limit=self.max_print)
        if not res["ok"]:
            err = res["error"] or ""
            if "not found in contract" in err:
                return True, None  # factory without a lifecycle view
            return False, f"factory active-check failed: {err}"
        if res["return"] is False:
            return False, "instance is disabled by its factory"
        return True, None

    # -- deploy ------------------------------------------------------------ #
    def deploy(self, code, creator, address, world_state, constructor=None,
               height=0, depth=0, factory_addr=None):
        """Create a contract at ``address`` and run its init code.

        Mutates ``world_state`` (creates the contract, runs init).  Returns a
        result dict; on failure the caller is expected to revert the state.
        ``factory_addr`` tags a contract issued by another contract (a factory
        child) and is committed in the contract record.
        """
        result = {"ok": False, "error": None, "output": "", "events": [],
                  "address": address, "transfers": [], "factory": factory_addr}
        ok, msg = validate_source(code)
        if not ok:
            result["error"] = msg
            return result

        world_state.create_contract(address, code, creator,
                                    factory=factory_addr)
        context, events, transfers = self.build_context(
            world_state, address, creator, 0, height,
            depth=depth, factory_addr=factory_addr)
        ctx = {k: v for k, v in context.items()}

        if constructor:
            # Expect an ``init`` function taking the constructor args.
            res = call_function(code, "init", list(constructor), ctx,
                                output_limit=self.max_print)
        else:
            res = exec_restricted(code, ctx, output_limit=self.max_print)

        result["output"] = res["output"]
        if not res["ok"]:
            result["error"] = res["error"]
            # Revert storage populated before failure.
            world_state.contracts.pop(address, None)
            return result

        result["ok"] = True
        result["events"] = events
        result["transfers"] = transfers
        result["storage"] = copy.deepcopy(world_state.contract_storage(address))
        return result

    # -- invoke (state-changing) ------------------------------------------- #
    def invoke(self, contract_addr, function, args, sender, value,
               world_state, height=0):
        contract = world_state.contract(contract_addr)
        result = {"ok": False, "error": None, "output": "", "events": [],
                  "return": None, "transfers": []}
        if not contract:
            result["error"] = f"contract {contract_addr} not found"
            return result
        allowed, reason = self.factory_allows(
            world_state, contract.get("factory"), contract_addr, height)
        if not allowed:
            result["error"] = reason
            return result
        context, events, transfers = self.build_context(
            world_state, contract_addr, sender, value, height)
        res = call_function(contract["code"], function, list(args), context,
                            output_limit=self.max_print)
        result["output"] = res["output"]
        if not res["ok"]:
            result["error"] = res["error"]
            return result
        result["ok"] = True
        result["return"] = ensure_jsonable(res["return"])
        result["events"] = events
        result["transfers"] = transfers
        return result

    # -- simulate (read-only, no mutation) --------------------------------- #
    def simulate(self, contract_addr, function, args, sender, world_state,
                 height=0):
        snapshot = world_state.copy()
        contract = snapshot.contract(contract_addr)
        result = {"ok": False, "error": None, "output": "", "events": [],
                  "return": None, "transfers": []}
        if not contract:
            result["error"] = f"contract {contract_addr} not found"
            return result
        allowed, reason = self.factory_allows(
            snapshot, contract.get("factory"), contract_addr, height)
        if not allowed:
            result["error"] = reason
            return result
        return self.invoke(contract_addr, function, args, sender, 0,
                           snapshot, height)
