"""Built-in smart-contract templates for the template library.

Each template is a complete, sandbox-valid contract written against the
contract API (``state``, ``msg``, ``emit``, ``require``, ``transfer``,
``balance_of``).  They are exposed on the template-library page and can be
deployed with one click.
"""

TEMPLATES = [
    {
        "name": "token",
        "title": "可替代代币 (ERC-20 风格)",
        "category": "金融",
        "description": "发行一种可转账的代币，包含铸造、转账、余额查询与总量查询。",
        "constructor": [
            {"name": "name", "type": "string", "desc": "代币名称"},
            {"name": "symbol", "type": "string", "desc": "代币符号"},
            {"name": "supply", "type": "int", "desc": "初始发行量"},
        ],
        "functions": [
            {"name": "transfer", "desc": "向指定地址转账", "params": ["to", "amount"]},
            {"name": "balance_of", "desc": "查询某地址余额", "params": ["addr"]},
            {"name": "total_supply", "desc": "查询代币总量", "params": []},
        ],
        "source": '''# 可替代代币模板 (ERC-20 风格)
def init(name, symbol, supply):
    require(state.get("name") is None, "合约已初始化")
    state["name"] = name
    state["symbol"] = symbol
    state["total_supply"] = supply
    state["bal_" + msg.sender] = supply
    emit("Minted", to=msg.sender, amount=supply)

def transfer(to, amount):
    amount = int(amount)
    require(amount > 0, "转账金额必须为正")
    bal = state.get("bal_" + msg.sender, 0)
    require(bal >= amount, "余额不足")
    state["bal_" + msg.sender] = bal - amount
    state["bal_" + to] = state.get("bal_" + to, 0) + amount
    emit("Transfer", frm=msg.sender, to=to, amount=amount)

def balance_of(addr):
    return state.get("bal_" + addr, 0)

def total_supply():
    return state.get("total_supply", 0)
''',
    },
    {
        "name": "kv_store",
        "title": "键值存储",
        "category": "存储",
        "description": "一个简单的持久化键值对存储，支持写入与读取。",
        "constructor": [],
        "functions": [
            {"name": "set", "desc": "写入键值", "params": ["key", "value"]},
            {"name": "get", "desc": "读取键值", "params": ["key"]},
        ],
        "source": '''# 键值存储模板
def init():
    state["owner"] = msg.sender
    state["count"] = 0
    emit("Initialized", owner=msg.sender)

def set(key, value):
    key = str(key)
    require(key != "", "键不能为空")
    state[key] = value
    state["count"] = state.get("count", 0) + 1
    emit("Set", key=key, value=value, by=msg.sender)

def get(key):
    return state.get(str(key), None)
''',
    },
    {
        "name": "voting",
        "title": "投票合约",
        "category": "治理",
        "description": "创建候选人、投票、查看票数。每个地址限投一次。",
        "constructor": [
            {"name": "candidates", "type": "list", "desc": "候选人列表，如 ['Alice','Bob']"},
        ],
        "functions": [
            {"name": "vote", "desc": "给候选人投票", "params": ["candidate"]},
            {"name": "tally", "desc": "查询候选人票数", "params": ["candidate"]},
        ],
        "source": '''# 投票合约模板
def init(candidates):
    require(state.get("owner") is None, "已初始化")
    state["owner"] = msg.sender
    state["candidates"] = list(candidates)
    for c in candidates:
        state["vote_" + str(c)] = 0
    emit("Created", candidates=candidates)

def vote(candidate):
    require(str(candidate) in state.get("candidates", []), "候选人不存在")
    require(state.get("voted_" + msg.sender, False) is False, "已投过票")
    state["voted_" + msg.sender] = True
    state["vote_" + str(candidate)] = state.get("vote_" + str(candidate), 0) + 1
    emit("Voted", voter=msg.sender, candidate=str(candidate))

def tally(candidate):
    return state.get("vote_" + str(candidate), 0)
''',
    },
    {
        "name": "escrow",
        "title": "托管合约",
        "category": "金融",
        "description": "买家存入资金，买家确认后资金释放给卖家，买家可申请退款。",
        "constructor": [
            {"name": "seller", "type": "address", "desc": "卖家地址"},
        ],
        "functions": [
            {"name": "deposit", "desc": "买家存入资金", "params": []},
            {"name": "release", "desc": "买家确认放款给卖家", "params": []},
            {"name": "refund", "desc": "买家申请退款", "params": []},
            {"name": "amount", "desc": "查询托管金额", "params": []},
        ],
        "source": '''# 托管合约模板
def init(seller):
    require(state.get("seller") is None, "已初始化")
    state["seller"] = seller
    state["buyer"] = msg.sender
    state["amount"] = 0
    state["released"] = False
    emit("Created", seller=seller, buyer=msg.sender)

def deposit():
    require(msg.sender == state["buyer"], "只有买家可存入")
    require(state["released"] is False, "合约已结束")
    state["amount"] = state.get("amount", 0) + msg.value
    emit("Deposited", by=msg.sender, amount=msg.value)

def release():
    require(msg.sender == state["buyer"], "只有买家可确认放款")
    require(state["released"] is False, "已放款")
    state["released"] = True
    transfer(state["seller"], state["amount"])
    emit("Released", seller=state["seller"], amount=state["amount"])

def refund():
    require(msg.sender == state["buyer"], "只有买家可退款")
    require(state["released"] is False, "已放款")
    state["released"] = True
    transfer(state["buyer"], state["amount"])
    emit("Refunded", buyer=state["buyer"], amount=state["amount"])

def amount():
    return state.get("amount", 0)
''',
    },
    {
        "name": "auction",
        "title": "拍卖合约",
        "category": "金融",
        "description": "英式拍卖：出价必须高于当前最高价，拍卖结束后最高出价者胜出。",
        "constructor": [
            {"name": "item", "type": "string", "desc": "拍卖品名称"},
            {"name": "starting_price", "type": "int", "desc": "起拍价"},
            {"name": "end_height", "type": "int", "desc": "结束区块高度"},
        ],
        "functions": [
            {"name": "bid", "desc": "出价（需附带 value）", "params": []},
            {"name": "highest_bidder", "desc": "查询最高出价者", "params": []},
            {"name": "highest_bid", "desc": "查询最高出价", "params": []},
        ],
        "source": '''# 拍卖合约模板
def init(item, starting_price, end_height):
    require(state.get("item") is None, "已初始化")
    state["item"] = item
    state["highest_bid"] = int(starting_price)
    state["highest_bidder"] = msg.sender
    state["end_height"] = int(end_height)
    state["ended"] = False
    emit("AuctionCreated", item=item, start=int(starting_price))

def bid():
    require(block_height < state["end_height"], "拍卖已结束")
    require(msg.value > state["highest_bid"], "出价必须高于当前最高价")
    prev_bidder = state["highest_bidder"]
    prev_bid = state["highest_bid"]
    # 退回上一出价人
    transfer(prev_bidder, prev_bid)
    state["highest_bid"] = msg.value
    state["highest_bidder"] = msg.sender
    emit("Bid", bidder=msg.sender, amount=msg.value)

def highest_bidder():
    return state["highest_bidder"]

def highest_bid():
    return state["highest_bid"]
''',
    },
    {
        "name": "crowdfunding",
        "title": "众筹合约",
        "category": "金融",
        "description": "众筹目标金额，支持出资与查询进度，达到目标后项目方可提现。",
        "constructor": [
            {"name": "goal", "type": "int", "desc": "众筹目标金额"},
        ],
        "functions": [
            {"name": "contribute", "desc": "出资（需附带 value）", "params": []},
            {"name": "progress", "desc": "查询已筹金额", "params": []},
            {"name": "withdraw", "desc": "项目方提现（需达到目标）", "params": []},
        ],
        "source": '''# 众筹合约模板
def init(goal):
    require(state.get("owner") is None, "已初始化")
    state["owner"] = msg.sender
    state["goal"] = int(goal)
    state["raised"] = 0
    state["withdrawn"] = False
    emit("CampaignStarted", goal=int(goal))

def contribute():
    require(state["withdrawn"] is False, "众筹已结束")
    state["raised"] = state.get("raised", 0) + msg.value
    state["contrib_" + msg.sender] = state.get("contrib_" + msg.sender, 0) + msg.value
    emit("Contribution", from_=msg.sender, amount=msg.value)

def progress():
    return state.get("raised", 0)

def withdraw():
    require(msg.sender == state["owner"], "只有项目方可提现")
    require(state["raised"] >= state["goal"], "未达到众筹目标")
    require(state["withdrawn"] is False, "已提现")
    state["withdrawn"] = True
    transfer(state["owner"], state["raised"])
    emit("Withdrawn", amount=state["raised"])
''',
    },
    {
        "name": "counter",
        "title": "计数器",
        "category": "基础",
        "description": "最简单的合约，演示状态持久化与事件。",
        "constructor": [],
        "functions": [
            {"name": "increment", "desc": "计数 +1", "params": []},
            {"name": "get", "desc": "查询当前计数", "params": []},
        ],
        "source": '''# 计数器模板
def init():
    state["count"] = 0
    emit("Created", by=msg.sender)

def increment():
    state["count"] = state.get("count", 0) + 1
    emit("Incremented", value=state["count"])

def get():
    return state.get("count", 0)
''',
    },
    {
        "name": "factory",
        "title": "合约工厂（批量发行 / 停用 / 启用）",
        "category": "工厂",
        "description": "登记同一种类子合约的源代码，一键批量发行实例，登记地址、创建者、"
                       "运行状态；创建者可停用/启用自己的实例；任何人可查看完整列表与统计。",
        "constructor": [
            {"name": "code", "type": "string", "desc": "要批量发行的子合约源代码"},
            {"name": "kind", "type": "string", "desc": "实例种类名称（可留空）"},
        ],
        "functions": [
            {"name": "issue", "desc": "发行一个实例，参数: 标签, 子合约构造参数...",
             "params": ["label", "..."]},
            {"name": "issue_many", "desc": "批量发行 n 个实例", "params": ["n", "label"]},
            {"name": "disable", "desc": "停用自己名下的实例", "params": ["instance"]},
            {"name": "enable", "desc": "重新启用自己名下的实例", "params": ["instance"]},
            {"name": "instance_info", "desc": "查询单个实例登记信息", "params": ["instance"]},
            {"name": "all_instances", "desc": "所有已发行实例（含创建者、状态）", "params": []},
            {"name": "instances_of", "desc": "查询某创建者发行的实例", "params": ["creator"]},
            {"name": "stats", "desc": "累计发行量 / 运行中 / 已停用", "params": []},
            {"name": "is_instance_active", "desc": "引擎查询实例是否运行中", "params": ["instance"]},
        ],
        "source": '''# 合约工厂：批量发行同一种类的合约实例，并登记实例生命周期
#
# 发行：调用 issue(标签, 子合约构造参数...) 即可一键创建一个子合约实例。
#       子合约地址由链上确定性推导，原始调用者被登记为实例创建者。
# 生命周期：实例创建者可随时 disable/enable 自己名下的实例；停用后引擎会
#       拒绝任何人（包括创建者）调用该实例，直到重新启用。
# 可见性：all_instances / stats 对所有用户开放，可区分每个实例的创建者。

def init(code, kind=""):
    require(state.get("is_factory") is None, "工厂已初始化")
    require(isinstance(code, str) and len(code.strip()) > 0, "子合约代码不能为空")
    state["is_factory"] = True
    state["child_code"] = code
    state["kind"] = str(kind or "")
    state["total"] = 0
    state["active_count"] = 0
    state["instances"] = []
    state["creator_idx"] = {}
    state["owner"] = msg.sender
    emit("FactoryCreated", kind=state["kind"], by=msg.sender)

def issue(label="", *args):
    """发行一个实例。第一个参数为实例标签，其余参数传给子合约 init。"""
    addr = deploy_contract(state["child_code"], *args)
    total = int(state.get("total", 0)) + 1
    state["total"] = total
    state["active_count"] = int(state.get("active_count", 0)) + 1
    record = {
        "address": addr,
        "creator": msg.sender,
        "label": str(label or ""),
        "active": True,
        "index": total,
    }
    instances = list(state.get("instances", []))
    instances.append(record)
    state["instances"] = instances
    idx = dict(state.get("creator_idx", {}))
    mine = list(idx.get(msg.sender, []))
    mine.append(addr)
    idx[msg.sender] = mine
    state["creator_idx"] = idx
    state["rec_" + addr] = record
    emit("Issued", address=addr, creator=msg.sender, index=total,
         label=record["label"])
    return addr

def issue_many(n, label=""):
    """一键批量发行 n 个相同种类的实例，返回新实例地址列表。"""
    n = int(n)
    require(n > 0, "数量必须为正")
    require(n <= 50, "单次最多发行 50 个实例")
    addresses = []
    i = 0
    while i < n:
        addresses.append(issue(label))
        i += 1
    emit("BatchIssued", creator=msg.sender, count=n)
    return addresses

def _require_owner_of(addr):
    rec = state.get("rec_" + str(addr))
    require(rec is not None, "实例不存在")
    require(rec.get("creator") == msg.sender, "只有实例创建者可操作")
    return rec

def disable(addr):
    """停用自己名下的实例（停用后该实例无法被调用）。"""
    rec = _require_owner_of(addr)
    require(rec.get("active") is True, "实例已处于停用状态")
    rec["active"] = False
    state["rec_" + str(addr)] = rec
    state["active_count"] = int(state.get("active_count", 0)) - 1
    instances = list(state.get("instances", []))
    for i in range(len(instances)):
        if instances[i].get("address") == str(addr):
            instances[i] = rec
    state["instances"] = instances
    emit("Disabled", address=addr, creator=msg.sender)

def enable(addr):
    """重新启用自己名下的实例。"""
    rec = _require_owner_of(addr)
    require(rec.get("active") is False, "实例已处于运行状态")
    rec["active"] = True
    state["rec_" + str(addr)] = rec
    state["active_count"] = int(state.get("active_count", 0)) + 1
    instances = list(state.get("instances", []))
    for i in range(len(instances)):
        if instances[i].get("address") == str(addr):
            instances[i] = rec
    state["instances"] = instances
    emit("Enabled", address=addr, creator=msg.sender)

def is_instance_active(addr):
    """引擎在调用每个实例前通过本视图判断其是否运行中。"""
    rec = state.get("rec_" + str(addr))
    if rec is None:
        return False
    return rec.get("active") is True

def instance_info(addr):
    return state.get("rec_" + str(addr), None)

def all_instances():
    """全部已发行实例的完整登记列表（含创建者与运行状态）。"""
    return list(state.get("instances", []))

def instances_of(creator):
    """查询某个创建者名下发行过的全部实例地址。"""
    return list(dict(state.get("creator_idx", {})).get(creator, []))

def stats():
    """整体统计：累计发行、运行中、已停用。"""
    total = int(state.get("total", 0))
    active = int(state.get("active_count", 0))
    return {"kind": state.get("kind", ""),
            "total": total,
            "active": active,
            "disabled": total - active}
''',
    },
    {
        "name": "service",
        "title": "可托管服务实例（工厂子合约示例）",
        "category": "工厂",
        "description": "适合被工厂批量发行的子合约：每个实例带名称与编号，"
                       "支持计数，初始化时记录所属工厂。",
        "constructor": [
            {"name": "name", "type": "string", "desc": "实例名称"},
        ],
        "functions": [
            {"name": "tick", "desc": "服务计数 +1", "params": []},
            {"name": "info", "desc": "查询实例信息", "params": []},
        ],
        "source": '''# 可托管服务实例：由工厂批量发行，记录创建者与所属工厂
def init(name="service"):
    state["name"] = str(name)
    state["ticks"] = 0
    state["creator"] = msg.sender
    state["factory"] = this_factory()
    emit("ServiceStarted", name=state["name"], by=msg.sender)

def tick():
    state["ticks"] = state.get("ticks", 0) + 1
    emit("Tick", name=state.get("name"), ticks=state["ticks"], by=msg.sender)

def info():
    return {"name": state.get("name"), "ticks": state.get("ticks", 0),
            "creator": state.get("creator"),
            "factory": state.get("factory")}
''',
    },
]


def get_templates():
    return TEMPLATES


def get_template(name):
    for t in TEMPLATES:
        if t["name"] == name:
            return t
    return None


def template_catalog():
    """Return templates without their source (for the list view)."""
    return [
        {
            "name": t["name"],
            "title": t["title"],
            "category": t["category"],
            "description": t["description"],
            "constructor": t["constructor"],
            "functions": t["functions"],
        }
        for t in TEMPLATES
    ]
