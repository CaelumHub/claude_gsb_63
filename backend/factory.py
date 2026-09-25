"""Contract factory: one-click batch issuance of same-kind contract instances.

The factory pairs an on-chain **registry contract** with this off-chain
orchestrator:

* **Issuance** — every instance is a real contract deployed from the same
  source (a built-in/custom template or raw code).  After deployment the
  factory registers all new instances in the registry contract, recording
  each instance's address, creator, kind, label, and a ``running`` status.
* **Status control** — the registry enforces *on-chain* that only an
  instance's creator may ``deactivate`` / ``reactivate`` it; anyone can
  read the full instance list and the aggregate stats.
* **Listing & stats** — the full list (with each instance's creator) and
  the overall statistics (total issued, currently running, stopped) are
  derived from the registry contract's storage, so they are covered by the
  state root and survive restarts / syncs.

Because the mempool admits only one pending transaction per sender and
nonces must be gap-free, the factory mines a block after every transaction
it submits: issuing ``N`` instances therefore produces ``N + 1`` blocks
(plus one more the very first time, when the registry itself is deployed).
"""

import os

from . import crypto
from .config import CONTRACT_ADDR_PREFIX
from .sandbox import validate_source
from .storage import atomic_write_json, read_json
from .templates import get_template

FACTORY_META_FILE = "factory.json"
CUSTOM_TEMPLATES_FILE = "custom_templates.json"
MAX_BATCH = 20

STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_ACTIONS = {"deactivate": STATUS_STOPPED, "reactivate": STATUS_RUNNING}

# The on-chain registry.  Deployed once per chain by :meth:`ensure_registry`;
# its storage layout is:
#   "total" / "running"   — counters (stopped = total - running)
#   "ids"                 — list of registered instance addresses (ordered)
#   "rec_<address>"       — per-instance record dict
REGISTRY_SOURCE = '''# 工厂注册表合约（由合约工厂自动部署）。
# 登记每个发行实例的地址、创建者与运行状态；创建者可停用 / 重新启用
# 自己名下的实例，任何人都可查询完整列表与整体统计。

def register_many(items):
    for it in items:
        register_one(it[0], it[1], it[2])

def register_one(instance, kind, label):
    require(state.get("rec_" + instance) is None, "实例已登记")
    idx = state.get("total", 0)
    state["rec_" + instance] = {
        "id": idx,
        "address": instance,
        "creator": msg.sender,
        "kind": str(kind),
        "label": str(label),
        "status": "running",
        "height": block_height,
    }
    state["ids"] = state.get("ids", []) + [instance]
    state["total"] = idx + 1
    state["running"] = state.get("running", 0) + 1
    emit("InstanceIssued", id=idx, address=instance,
         creator=msg.sender, kind=str(kind))

def deactivate(instance):
    set_status(instance, "stopped")

def reactivate(instance):
    set_status(instance, "running")

def set_status(instance, status):
    require(status == "running" or status == "stopped", "非法状态")
    rec = state.get("rec_" + instance)
    require(rec is not None, "实例未登记")
    require(msg.sender == rec["creator"], "只有实例创建者可以操作")
    require(rec["status"] != status, "实例已处于该状态")
    rec["status"] = status
    state["rec_" + instance] = rec
    if status == "running":
        state["running"] = state.get("running", 0) + 1
    else:
        state["running"] = state.get("running", 0) - 1
    emit("InstanceStatusChanged", address=instance,
         status=status, by=msg.sender)

def get(instance):
    return state.get("rec_" + instance)

def list_all():
    out = []
    for addr in state.get("ids", []):
        rec = state.get("rec_" + addr)
        if rec is not None:
            out.append(rec)
    return out

def list_by_creator(creator):
    out = []
    for addr in state.get("ids", []):
        rec = state.get("rec_" + addr)
        if rec is not None and rec["creator"] == creator:
            out.append(rec)
    return out

def stats():
    total = state.get("total", 0)
    running = state.get("running", 0)
    return {"total": total, "running": running, "stopped": total - running}
'''


class FactoryError(Exception):
    """Raised for any factory-operation failure (surfaced as a 400)."""


def _contract_address(tx):
    """Derive a contract address from its deploy tx (mirrors the chain)."""
    return CONTRACT_ADDR_PREFIX + crypto.sha256(tx.txid.encode()).hex()[:40]


class ContractFactory:
    """Orchestrates issuance and tracks the on-chain registry contract."""

    def __init__(self, node):
        self.node = node

    # ------------------------------------------------------------------ #
    # Registry contract discovery / deployment
    # ------------------------------------------------------------------ #
    def _meta_path(self):
        return os.path.join(self.node.paths.root, FACTORY_META_FILE)

    def _load_meta(self):
        return read_json(self._meta_path(), {}) or {}

    def _save_meta(self, meta):
        atomic_write_json(self._meta_path(), meta)

    def registry_address(self):
        """Address of the live registry contract, or ``None`` if not deployed.

        The address is remembered in ``factory.json`` but only honoured while
        the contract actually exists in the world state (a chain reset or a
        rollback past the deploy height invalidates it).  If the local memo is
        missing/stale — e.g. this node only *synced* the chain from a peer —
        the registry is rediscovered on-chain by matching the registry source,
        preferring the one with the most registered instances so every node
        deterministically converges on the same registry.
        """
        addr = self._load_meta().get("registry")
        if addr and self.node.blockchain.state.contract(addr):
            return addr
        best, best_count = None, -1
        for cand, contract in self.node.blockchain.state.contracts.items():
            if contract.get("code") != REGISTRY_SOURCE:
                continue
            count = len(contract.get("storage", {}).get("ids", []))
            if count > best_count or (count == best_count
                                      and best is not None and cand < best):
                best, best_count = cand, count
        if best:
            meta = self._load_meta()
            meta["registry"] = best
            self._save_meta(meta)
        return best

    def ensure_registry(self, sender):
        """Return the registry address, deploying it (as ``sender``) if needed."""
        addr = self.registry_address()
        if addr:
            return addr
        tx, err = self.node.create_deploy(sender, REGISTRY_SOURCE, 0.0)
        if err:
            raise FactoryError(f"无法创建注册表部署交易: {err}")
        receipt = self._submit_and_mine(tx, sender)
        if not receipt.get("ok"):
            raise FactoryError(f"注册表部署失败: {receipt.get('error')}")
        addr = _contract_address(tx)
        if not self.node.blockchain.state.contract(addr):
            raise FactoryError("注册表部署未生效")
        meta = self._load_meta()
        meta["registry"] = addr
        self._save_meta(meta)
        self.node.log("info", f"factory registry deployed at {addr[:18]}…")
        return addr

    # ------------------------------------------------------------------ #
    # Issuance
    # ------------------------------------------------------------------ #
    def _resolve_source(self, template, source):
        """Return ``(source, kind, default_label)`` for a template or raw code."""
        if template:
            tpl = get_template(template)
            if tpl is None:
                custom = read_json(os.path.join(
                    self.node.paths.root, CUSTOM_TEMPLATES_FILE), [])
                tpl = next((t for t in custom if t.get("name") == template), None)
            if tpl is None:
                raise FactoryError(f"模板不存在: {template}")
            return tpl["source"], template, tpl.get("title", template)
        if source and source.strip():
            return source, "custom", "自定义合约"
        raise FactoryError("必须指定模板或合约源码")

    def issue(self, sender, template=None, source=None, constructor=None,
              label="", count=1, fee=0.0):
        """Deploy ``count`` instances of one contract kind and register them.

        Every instance is a real deployed contract; all instances of the
        batch are then recorded in the registry with status ``running``.
        Returns a dict with the registry address and the issued instances.
        """
        source, kind, default_label = self._resolve_source(template, source)
        ok, msg = validate_source(source)
        if not ok:
            raise FactoryError(f"合约源码校验失败: {msg}")
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise FactoryError("发行数量必须是整数")
        if not 1 <= count <= MAX_BATCH:
            raise FactoryError(f"单次发行数量须在 1–{MAX_BATCH} 之间")
        if constructor is not None and not isinstance(constructor, list):
            raise FactoryError("构造参数必须是数组")
        label = str(label or "").strip() or default_label

        registry = self.ensure_registry(sender)

        issued = []
        deploy_error = None
        for i in range(count):
            tx, err = self.node.create_deploy(sender, source, fee,
                                              constructor=constructor)
            if err:
                deploy_error = err
                break
            try:
                receipt = self._submit_and_mine(tx, sender)
            except FactoryError as e:
                deploy_error = str(e)
                break
            if not receipt.get("ok"):
                deploy_error = receipt.get("error") or "部署执行失败"
                break
            addr = _contract_address(tx)
            issued.append({
                "address": addr,
                "txid": tx.txid,
                "label": f"{label} #{i + 1}" if count > 1 else label,
            })

        # Register everything that deployed successfully (even if the batch
        # aborted part-way, issued instances must show up in the factory).
        register_txid = None
        if issued:
            items = [[it["address"], kind, it["label"]] for it in issued]
            tx, err = self.node.create_call(sender, registry, "register_many",
                                            [items], fee=fee)
            if err:
                raise FactoryError(f"实例已部署但登记交易创建失败: {err}")
            receipt = self._submit_and_mine(tx, sender)
            if not receipt.get("ok"):
                raise FactoryError(
                    f"实例已部署但登记失败: {receipt.get('error')}")
            register_txid = tx.txid

        result = {"registry": registry, "kind": kind, "issued": issued,
                  "register_txid": register_txid}
        if deploy_error:
            if not issued:
                raise FactoryError(deploy_error)
            result["warning"] = (f"发行 {len(issued)} 个实例后中止: "
                                 f"{deploy_error}")
        return result

    # ------------------------------------------------------------------ #
    # Status control (creator-only, enforced on-chain)
    # ------------------------------------------------------------------ #
    def set_status(self, sender, instance, action, fee=0.0):
        """Deactivate/reactivate ``instance``; ``sender`` must be its creator."""
        if action not in STATUS_ACTIONS:
            raise FactoryError("action 必须是 deactivate 或 reactivate")
        registry = self.registry_address()
        if not registry:
            raise FactoryError("工厂尚未初始化（还没有发行过实例）")
        bc = self.node.blockchain
        # Dry-run first so the caller gets immediate feedback without
        # wasting a block on a call that would revert.
        sim = bc.engine.simulate(registry, action, [instance], sender,
                                 bc.state, bc.height)
        if not sim["ok"]:
            raise FactoryError(sim["error"] or "状态变更预检失败")
        tx, err = self.node.create_call(sender, registry, action,
                                        [instance], fee=fee)
        if err:
            raise FactoryError(err)
        receipt = self._submit_and_mine(tx, sender)
        if not receipt.get("ok"):
            raise FactoryError(receipt.get("error") or "状态变更执行失败")
        return {"txid": tx.txid, "address": instance,
                "status": STATUS_ACTIONS[action]}

    # ------------------------------------------------------------------ #
    # Listing / statistics (read from registry storage)
    # ------------------------------------------------------------------ #
    def _registry_storage(self):
        addr = self.registry_address()
        if not addr:
            return {}
        contract = self.node.blockchain.state.contract(addr)
        return contract["storage"] if contract else {}

    def instances(self, creator=None, status=None):
        """Full list of registered instances, optionally filtered."""
        st = self.node.blockchain.state
        storage = self._registry_storage()
        out = []
        for addr in storage.get("ids", []):
            rec = storage.get("rec_" + addr)
            if not isinstance(rec, dict):
                continue
            rec = dict(rec)
            rec["balance"] = st.balance(addr)
            rec["deployed"] = st.contract(addr) is not None
            if creator and rec.get("creator") != creator:
                continue
            if status and rec.get("status") != status:
                continue
            out.append(rec)
        out.sort(key=lambda r: r.get("id", 0))
        return out

    def stats(self):
        """Aggregate stats: total issued, running, stopped, per-creator."""
        records = self.instances()
        running = sum(1 for r in records
                      if r.get("status") == STATUS_RUNNING)
        by_creator = {}
        for r in records:
            entry = by_creator.setdefault(r.get("creator"), {
                "creator": r.get("creator"), "total": 0,
                "running": 0, "stopped": 0,
            })
            entry["total"] += 1
            if r.get("status") == STATUS_RUNNING:
                entry["running"] += 1
            else:
                entry["stopped"] += 1
        return {
            "total": len(records),
            "running": running,
            "stopped": len(records) - running,
            "creators": len(by_creator),
            "by_creator": sorted(by_creator.values(),
                                 key=lambda e: (-e["total"],
                                                str(e["creator"]))),
        }

    # ------------------------------------------------------------------ #
    # Tx helper: submit then immediately confirm into a block
    # ------------------------------------------------------------------ #
    def _submit_and_mine(self, tx, sender):
        """Submit ``tx`` and mine it into a block; return its receipt."""
        ok, reason = self.node.submit_transaction(tx)
        if not ok:
            raise FactoryError(reason)
        status, message, _height = self.node.mine_block(sender)
        if status != "extended":
            raise FactoryError(f"出块失败: {message}")
        for receipt in self.node.blockchain.last_receipts:
            if receipt.get("txid") == tx.txid:
                return receipt
        raise FactoryError("交易未被打包确认")
