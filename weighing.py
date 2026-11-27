"""按生效秤档、稳定窗口、去皮与容器基准，把原始观测解释成交接净重。

判定原则（对应 protocol.md 与 policy.json）：

读数与秤档
  * ``observe`` 自报的 ``mode`` 只有在本次开机登记的秤档与它一致时才生效；
    否则按开机档解读并留下提示。量程按 *生效档* 检查，显示值须落在该档的
    最小显示步长网格上。稳定窗口须不短于设备要求的 ``stable_ms``。
  * 只有“时长、量程、显示步长”三项都成立的数字才算稳定数字。

去皮
  * 秤端去皮只有在同一次开机内、该 ``zero_id`` 的最近阶段为 ``effective``
    时生效。``received`` 仅表示收到命令；零点定义之后若设备重连，旧零点失效。
  * 生效的秤端去皮已经把该重量从显示值里扣掉，独立登记的容器皮重 *不得再减*。
  * 未生效的去皮不得被悄悄当成已扣皮 —— 这样的读数只作待查。

容器
  * 标签给出称量期间同在台面的嵌套组合（父筐可套副筐）。标签按时间取
    “最近一次不晚于该称量的”组合。
  * 独立登记的已知皮重只在容器身份与干湿条件都相符时可用；某条件未登记即
    不可用。标签缺失时读数只能待查（“只看到稳定数字但不知道加了哪些物件”）。

交接与分装
  * 两次称量只有容器组合与干湿条件一致（同一实物基准）时才可按差值办理。
    中途换掉一个子筐则基准断裂，原交接仍可重放，但差值不能直接沿用。
  * 已知皮重是区间时，单次净重是区间；对外净重只舍入一次（半步进位），
    并同时保留舍入后的值与未舍入的核验原值。
  * 结单必须显式引用前后两次称量；设备上的去皮不会被自动归到任何一张单。
  * 剩余物校验比较“旧结单之后的余量”与“换容器后的新称量”：新称量的净重
    区间若覆盖旧余量（容差为法定步长），则新旧互证、可继续办；否则明确
    指出哪一段读数需要补称，且不得超出已结清的净重。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent

CLEAN = "clean"       # 依据完整，可按此办理
NOTICE = "notice"     # 有需留意之处，但依据仍可用于办理
OPEN = "open"         # 该任务尚未结单，等待后续交接
HOLD = "hold"         # 有数字但不能据此办理，须补证
BROKEN = "broken"     # 实物基准断裂，只能办理证据连续的部分

_SEVERITY = {CLEAN: 0, NOTICE: 1, OPEN: 1, HOLD: 2, BROKEN: 3}
USABLE = (CLEAN, NOTICE)


def worst(*states: str) -> str:
    return max(states, key=lambda s: _SEVERITY[s])


# --------------------------------------------------------------------------- #
# 精确数值
# --------------------------------------------------------------------------- #

def parse_value(value: Any) -> Fraction:
    """JSON 数值/字符串 -> 精确有理数。"""

    if isinstance(value, bool):
        raise ValueError("布尔值不是重量")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        try:
            number = Decimal(repr(value))
        except InvalidOperation as exc:  # pragma: no cover
            raise ValueError("无法解析重量") from exc
        if not number.is_finite():
            raise ValueError("非有限重量")
        return Fraction(number)
    if isinstance(value, str):
        try:
            return Fraction(Decimal(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"无法解析重量：{value!r}") from exc
    raise ValueError(f"不支持的重量类型：{type(value).__name__}")


def round_half_up(value: Fraction, step: Fraction) -> Fraction:
    """舍入到 step 的整数倍，半步远离零（对外输出专用）。"""

    if step <= 0:
        raise ValueError("步长须为正")
    scaled = value / step
    sign = -1 if scaled < 0 else 1
    magnitude = abs(scaled)
    whole = magnitude.numerator // magnitude.denominator
    if magnitude - whole >= Fraction(1, 2):
        whole += 1
    return sign * whole * step


def on_step(value: Fraction, step: Fraction) -> bool:
    if step <= 0:
        return False
    return (value / step).denominator == 1


def fmt(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    decimal = Decimal(value.numerator) / Decimal(value.denominator)
    return format(decimal.normalize(), "f")


# --------------------------------------------------------------------------- #
# 台账
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ModeSpec:
    name: str
    step: Fraction
    low: Fraction
    high: Fraction


@dataclass(frozen=True)
class ScaleSpec:
    modes: Dict[str, ModeSpec]
    stable_ms: int


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    ranges: Dict[str, Tuple[Fraction, Fraction]]

    def range_for(self, condition: Optional[str]) -> Optional[Tuple[Fraction, Fraction]]:
        return self.ranges.get(condition) if condition else None


def _range(raw: Any) -> Tuple[Fraction, Fraction]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError("皮重区间须为 [下界, 上界]")
    low, high = parse_value(raw[0]), parse_value(raw[1])
    if low > high:
        raise ValueError("皮重区间下界大于上界")
    return (low, high)


class Ledger:
    """设备能力 / 现场合同。只读，可按项目替换。"""

    def __init__(self, devices: Dict[str, Any], policy: Dict[str, Any]) -> None:
        raw_scale = devices["scale"]
        modes = {
            name: ModeSpec(name, parse_value(spec["step_g"]), parse_value(spec["min_g"]), parse_value(spec["max_g"]))
            for name, spec in raw_scale["modes"].items()
        }
        if not modes:
            raise ValueError("设备表缺少秤档")
        self.scale = ScaleSpec(modes=modes, stable_ms=int(raw_scale.get("stable_ms", 0)))
        self.legal_step = parse_value(policy.get("legal_step_g", 1))
        self.containers = {
            name: ContainerSpec(name, {cond: _range(rng) for cond, rng in spec.items()})
            for name, spec in policy.get("containers", {}).items()
        }

    @classmethod
    def load(cls, devices: Optional[Path] = None, policy: Optional[Path] = None) -> "Ledger":
        devices = devices or ROOT / "devices.json"
        policy = policy or ROOT / "policy.json"
        return cls(
            json.loads(Path(devices).read_text(encoding="utf-8")),
            json.loads(Path(policy).read_text(encoding="utf-8")),
        )

    def mode(self, name: Optional[str]) -> Optional[ModeSpec]:
        return self.scale.modes.get(name) if name else None


# --------------------------------------------------------------------------- #
# 场景索引
# --------------------------------------------------------------------------- #

def load_observations(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = path or ROOT / "observations.ndjson"
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if [r["seq"] for r in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("观测序号须连续")
    return rows


class Scene:
    def __init__(self, rows: Sequence[Dict[str, Any]], ledger: Ledger) -> None:
        self.rows = list(rows)
        self.ledger = ledger
        self.by_seq = {r["seq"]: r for r in self.rows}
        self.boots: List[Dict[str, Any]] = [r for r in self.rows if r["kind"] == "boot"]
        self.tags: List[Dict[str, Any]] = []
        self.transfers: List[Dict[str, Any]] = []
        self.bills: List[Dict[str, Any]] = []
        self.validations: List[Dict[str, Any]] = []
        for row in self.rows:
            kind = row["kind"]
            if kind == "tag":
                self.tags.append(row)
            elif kind == "transfer":
                self.transfers.append(row)
            elif kind == "bill":
                self.bills.append(row)
            elif kind == "validate_remaining":
                self.validations.append(row)

    # -- 设备事实 -------------------------------------------------------- #

    def boot(self, boot_id: Optional[str]) -> Optional[Dict[str, Any]]:
        candidates = [b for b in self.boots if b["boot"] == boot_id]
        return candidates[-1] if candidates else None

    def boot_before(self, seq: int) -> Optional[Dict[str, Any]]:
        prior = [b for b in self.boots if b["seq"] < seq]
        return prior[-1] if prior else None

    def effective_mode(self, row: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
        problems: List[str] = []
        boot = self.boot(row.get("boot"))
        if boot is None:
            return self.ledger.mode(row.get("mode")) and row.get("mode"), [
                f"seq {row['seq']} 引用了未登记的开机 {row.get('boot')!r}"
            ]
        boot_mode = boot.get("mode")
        claimed = row.get("mode")
        if self.ledger.mode(boot_mode) is None:
            return None, [f"seq {row['seq']} 的开机秤档 {boot_mode!r} 不在设备表内"]
        if claimed != boot_mode:
            problems.append(
                f"seq {row['seq']} 自报秤档 {claimed!r} 与开机档 {boot_mode!r} 不符，按开机档解读"
            )
        return boot_mode, problems

    def effective_zero(self, row: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """该读数所声称的去皮是否真正生效。"""

        if not row.get("net"):
            return False, []
        zid = row.get("zero_id")
        problems: List[str] = []
        if not zid:
            return False, [f"seq {row['seq']} 声称去皮却没有零点标识"]
        # 取该读数之前最近一次定义的零点（同一标识可被重设）
        tares = [
            t for t in self.rows
            if t["kind"] == "tare" and t.get("zero_id") == zid and t["seq"] < row["seq"]
        ]
        if not tares:
            return False, [f"seq {row['seq']} 使用了未登记的零点 {zid!r}"]
        tare = tares[-1]
        if tare.get("stage") != "effective":
            return False, [
                f"seq {row['seq']} 的零点 {zid!r} 停留在 {tare.get('stage')!r}，去皮未生效"
            ]
        if tare.get("boot") != row.get("boot"):
            return False, [f"seq {row['seq']} 的零点 {zid!r} 属于另一次开机，不能沿用"]
        reconnect = [b for b in self.boots if tare["seq"] < b["seq"] < row["seq"]]
        if reconnect:
            return False, [
                f"seq {row['seq']} 在零点 {zid!r} 之后重连（seq {reconnect[-1]['seq']}），零点失效"
            ]
        return True, problems

    def tare_of(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        zid = row.get("zero_id")
        if not zid:
            return None
        tares = [
            t for t in self.rows
            if t["kind"] == "tare" and t.get("zero_id") == zid and t["seq"] < row["seq"]
        ]
        return tares[-1] if tares else None

    # -- 标签 ------------------------------------------------------------ #

    def tag_for(self, load: Optional[str], through: int) -> Optional[Dict[str, Any]]:
        """最近一次不晚于该称量结束的容器组合。

        标签给出称量期间确证同在台面的嵌套组合；取结束时刻之前最近的一次，
        这样换筐后发生的标签不会倒灌到更早的读数上。
        """

        tags = [t for t in self.tags if t.get("load") == load and t["at"] <= through]
        return tags[-1] if tags else None


# --------------------------------------------------------------------------- #
# 读数
# --------------------------------------------------------------------------- #

@dataclass
class Reading:
    seq: int
    at: int
    through: int
    load: Optional[str]
    boot: Optional[str]
    mode: Optional[str]
    step: Optional[Fraction]
    shown: Fraction
    gross: Fraction
    base: Fraction                 # 秤端已扣并生效的皮重
    net_claimed: bool
    zero_effective: bool
    side_touch: bool
    stable: bool
    status: str
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def window_ms(self) -> int:
        return self.through - self.at


class Check:
    """一次称量连同容器基准与净重区间。"""

    def __init__(
        self,
        reading: Reading,
        container: Optional["ContainerUse"],
        low: Optional[Fraction],
        high: Optional[Fraction],
        exact: bool,
        status: str,
        problems: List[str],
        notes: List[str],
    ) -> None:
        self.reading = reading
        self.container = container
        self.low = low
        self.high = high
        self.exact = exact
        self.status = status
        self.problems = problems
        self.notes = notes

    def net_text(self) -> Optional[str]:
        if self.low is None:
            return None
        if self.exact:
            return fmt(self.low)
        return f"{fmt(self.low)}~{fmt(self.high)}"


@dataclass
class ContainerUse:
    seq: int
    load: Optional[str]
    at: int
    tree: Dict[str, List[str]]
    condition: Dict[str, str]
    known: Dict[str, Tuple[Fraction, Fraction]] = field(default_factory=dict)
    usable: bool = True
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def names(self) -> List[str]:
        out: List[str] = []
        for parent, children in self.tree.items():
            for name in [parent, *children]:
                if name not in out:
                    out.append(name)
        return out

    def total_range(self) -> Tuple[Fraction, Fraction]:
        low = sum((r[0] for r in self.known.values()), Fraction(0))
        high = sum((r[1] for r in self.known.values()), Fraction(0))
        return low, high


def resolve_container(scene: Scene, row: Dict[str, Any]) -> ContainerUse:
    tag = scene.tag_for(row.get("load"), row["through"])
    if tag is None:
        return ContainerUse(
            seq=-1, load=row.get("load"), at=row["at"], tree={}, condition={},
            usable=False, problems=[f"seq {row['seq']} 期间没有容器标签，实物基准不明"],
        )
    use = ContainerUse(
        seq=tag["seq"], load=tag["load"], at=tag["at"],
        tree=tag.get("tree", {}), condition=tag.get("condition", {}),
    )
    children = [c for cs in use.tree.values() for c in cs]
    if len(children) != len(set(children)):
        use.problems.append("副筐在组合中被重复挂载")
        use.usable = False
    known = set(scene.ledger.containers)
    for name in use.names():
        if name not in known:
            use.problems.append(f"容器 {name!r} 未在合同登记")
            use.usable = False
            continue
        spec = scene.ledger.containers[name]
        condition = use.condition.get(name)
        rng = spec.range_for(condition)
        if rng is None:
            use.problems.append(
                f"容器 {name!r} 在条件 {condition!r} 下没有登记皮重，不能使用"
            )
            use.usable = False
            continue
        use.known[name] = rng
    return use


def read_one(scene: Scene, seq: int) -> Check:
    row = scene.by_seq[seq]
    if row["kind"] != "observe":
        raise ValueError(f"seq {seq} 不是称量观测")
    problems: List[str] = []
    notes: List[str] = []
    status = CLEAN

    mode, mode_problems = scene.effective_mode(row)
    problems.extend(mode_problems)
    status = worst(status, NOTICE if mode_problems else CLEAN)
    spec = scene.ledger.mode(mode)
    step = spec.step if spec else None

    shown = parse_value(row["shown_g"])
    zero_ok, zero_problems = scene.effective_zero(row)
    if zero_problems:
        problems.extend(zero_problems)
        status = worst(status, HOLD)
    base = Fraction(0)
    if zero_ok:
        tare = scene.tare_of(row)
        base = parse_value(tare.get("base_g", 0)) if tare else Fraction(0)
    gross = shown + base

    window = row.get("through", 0) - row.get("at", 0)
    if window < scene.ledger.scale.stable_ms:
        problems.append(
            f"seq {seq} 稳定时长 {window}ms 短于门槛 {scene.ledger.scale.stable_ms}ms"
        )
        status = worst(status, HOLD)
    if spec is None:
        problems.append(f"seq {seq} 无可用秤档，读数不可解读")
        status = worst(status, HOLD)
    else:
        if not (spec.low <= gross <= spec.high):
            problems.append(
                f"seq {seq} 毛重 {fmt(gross)}g 超出 {mode} 档量程 "
                f"[{fmt(spec.low)}, {fmt(spec.high)}]g"
            )
            status = worst(status, HOLD)
        if not on_step(shown, step):
            problems.append(
                f"seq {seq} 显示值 {fmt(shown)}g 不在 {mode} 档步长 {fmt(step)}g 的网格上"
            )
            status = worst(status, HOLD)
        if not on_step(gross, step):
            problems.append(
                f"seq {seq} 生效跨度 {fmt(gross)}g 不在 {mode} 档步长 {fmt(step)}g 的网格上，"
                "说明秤端扣除的皮重来自另一档或另一跨度"
            )
            status = worst(status, HOLD)

    stable = status in USABLE and spec is not None
    if row.get("side_touch"):
        problems.append(f"seq {seq} 记录到台面旁路接触，读数不能作为干净的增减依据")
        status = worst(status, HOLD)
        stable = False

    # 声称去皮但零点无效：显示值按毛重解读，且不计入任何净重
    if row.get("net") and not zero_ok:
        status = worst(status, HOLD)

    reading = Reading(
        seq=seq, at=row["at"], through=row["through"], load=row.get("load"),
        boot=row.get("boot"), mode=mode, step=step, shown=shown, gross=gross,
        base=base, net_claimed=bool(row.get("net")), zero_effective=zero_ok,
        side_touch=bool(row.get("side_touch")), stable=stable,
        status=status, problems=problems, notes=notes,
    )
    container = resolve_container(scene, row)
    for problem in container.problems:
        problems.append(problem)
    if not container.usable:
        status = worst(status, HOLD)
    if container.notes:
        notes.extend(container.notes)

    low: Optional[Fraction] = None
    high: Optional[Fraction] = None
    exact = False

    if container.usable:
        c_low, c_high = container.total_range()
        if zero_ok:
            # 秤端已扣 base；核对它是否与当前容器身份/条件相符，绝不再减一次
            if not (c_low <= base <= c_high):
                problems.append(
                    f"seq {seq} 秤端皮重 {fmt(base)}g 与标签容器合计 "
                    f"{fmt(c_low)}~{fmt(c_high)}g 不符（身份或干湿条件不符）"
                )
                status = worst(status, HOLD)
            low = high = gross - base
            exact = True
            # 秤端皮重只属于本次开机，不得自行并入任何单据
            notes.append(
                f"seq {seq} 秤端去皮 {fmt(base)}g 已生效，仅作本读数扣减；"
                "任何结单须显式引用本条"
            )
        else:
            # 未生效的去皮不得再减一次；只有独立登记的容器皮重可用
            low = gross - c_high
            high = gross - c_low
            exact = low == high
            if low < 0:
                problems.append(f"seq {seq} 扣除容器皮重后净重为负，读数不可作为食品依据")
                status = worst(status, HOLD)
    else:
        status = worst(status, HOLD)

    # 声称已去皮而零点无效：读数口径不明，给出区间作参考但不计入净重
    if row.get("net") and not zero_ok:
        low = high = None
        exact = False
        notes.append(f"seq {seq} 的去皮未生效，显示值口径不明，只能留待补证")

    return Check(
        reading=reading, container=container, low=low, high=high, exact=exact,
        status=status, problems=problems, notes=notes,
    )


# --------------------------------------------------------------------------- #
# 交接裁定
# --------------------------------------------------------------------------- #

def same_basis(a: Check, b: Check) -> List[str]:
    """两次称量是否处在同一实物基准。"""

    problems: List[str] = []
    ca, cb = a.container, b.container
    if ca is None or cb is None or not ca.usable or not cb.usable:
        return ["缺少可用的容器标签，无法确认同一实物基准"]
    if a.reading.load != b.reading.load:
        problems.append(f"载具不同：{a.reading.load!r} vs {b.reading.load!r}")
    if ca.tree != cb.tree:
        problems.append(f"容器组合变化：{json.dumps(ca.tree, ensure_ascii=False)} -> {json.dumps(cb.tree, ensure_ascii=False)}")
    if ca.condition != cb.condition:
        problems.append(
            f"干湿条件变化：{json.dumps(ca.condition, ensure_ascii=False)} -> "
            f"{json.dumps(cb.condition, ensure_ascii=False)}"
        )
    return problems


@dataclass
class Result:
    task: Optional[str]
    load: Optional[str]
    status: str
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    record: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "load": self.load,
            "status": self.status,
            "record": self.record,
            "problems": self.problems,
            "notes": self.notes,
        }


class Verifier:
    def __init__(self, ledger: Optional[Ledger] = None, rows: Optional[Sequence[Dict[str, Any]]] = None) -> None:
        self.ledger = ledger or Ledger.load()
        self.rows = list(rows) if rows is not None else load_observations()
        self.scene = Scene(self.rows, self.ledger)

    # -- 单次称量 -------------------------------------------------------- #

    def check(self, seq: int) -> Check:
        return read_one(self.scene, seq)

    def explain(self, seq: int) -> Dict[str, Any]:
        check = self.check(seq)
        r = check.reading
        return {
            "seq": seq,
            "at": r.at,
            "through": r.through,
            "window_ms": r.window_ms,
            "load": r.load,
            "boot": r.boot,
            "mode": r.mode,
            "step_g": fmt(r.step) if r.step is not None else None,
            "shown_g": fmt(r.shown),
            "tare_base_g": fmt(r.base),
            "gross_g": fmt(r.gross),
            "zero_effective": r.zero_effective,
            "stable": r.stable,
            "side_touch": r.side_touch,
            "containers": check.container.tree if check.container else None,
            "condition": check.container.condition if check.container else None,
            "net_g": check.net_text(),
            "home_basis": check.exact,
            "status": check.status,
            "problems": check.problems,
            "notes": check.notes,
        }

    # -- 结单 ------------------------------------------------------------ #

    def settle(self, task: str) -> Result:
        bill = self._last(lambda b: b.get("task") == task, self.scene.bills)
        if bill is not None:
            return self._settle_bill(bill)
        transfers = [t for t in self.scene.transfers if t.get("task") == task]
        if not transfers:
            if any(r.get("task") == task for r in self.rows if r["kind"] == "open"):
                return Result(
                    task=task, load=None, status=OPEN,
                    notes=["该任务已开单但尚无交接或结单，等待后续称量"],
                )
            return Result(task=task, load=None, status=HOLD, problems=[f"任务 {task} 没有交接记录"])
        return self._settle_transfers(task, transfers)

    def _settle_bill(self, bill: Dict[str, Any]) -> Result:
        result = Result(task=bill["task"], load=bill["load"], status=CLEAN)
        refs = list(bill.get("refs", []))
        if len(refs) != 2:
            result.status = HOLD
            result.problems.append("结单须显式引用前后两次称量")
            return result
        first, second = self.check(refs[0]), self.check(refs[1])
        # 基准断裂优先：它能直接说清哪一段要补称，不被单条读数的皮重提示掩盖
        basis = same_basis(first, second)
        if basis:
            result.status = BROKEN
            result.problems.extend(basis)
            result.problems.append(
                f"原先的交接仍可按 seq {refs[0]} 与 seq {refs[1]} 重放，但两次称量不属同一实物基准，"
                f"须对 seq {refs[1]} 的剩余物按当前容器补称后再办"
            )
            return result
        for item in (first, second):
            result.problems.extend(item.problems)
            if item.status not in USABLE:
                result.status = worst(result.status, HOLD)
            elif item.status == NOTICE:
                result.status = worst(result.status, NOTICE)
        if result.status not in USABLE:
            return result
        if first.low is None or second.high is None:
            result.status = HOLD
            result.problems.append("缺少净重，无法按差量结单")
            return result
        raw_low = first.low - second.high
        raw_high = first.high - second.low
        if raw_high <= 0:
            result.status = HOLD
            result.problems.append("差值不为正，不能作为交出的净重")
            return result
        settled = round_half_up(raw_low, self.ledger.legal_step)
        declared = bill.get("net_g")
        signers = self._signers(bill, first, second)
        record = {
            "task": bill["task"],
            "load": bill["load"],
            "before": refs[0],
            "after": refs[1],
            "net_g": fmt(settled),
            "net_raw_g": fmt(raw_low),
            "net_raw_interval_g": [fmt(raw_low), fmt(raw_high)],
            "legal_step_g": fmt(self.ledger.legal_step),
            "declared_g": declared,
            "containers": first.container.tree if first.container else None,
            "signers": signers,
        }
        result.record = record
        if declared is not None and parse_value(declared) != settled:
            result.status = worst(result.status, NOTICE)
            result.problems.append(
                f"对外结单 {declared}g 与原始称量核算 {fmt(settled)}g 不一致"
            )
        else:
            result.notes.append("对外净重与原始称量核算一致")
        if first.status == NOTICE or second.status == NOTICE:
            result.notes.append("所称量有需留意之处，已保留原始读数与核验原值")
        return result

    def _signers(self, bill: Dict[str, Any], *checks: Check) -> List[Dict[str, Any]]:
        """谁用哪组设备读数签收：把结单与它引用的称量、设备、容器串起来。"""

        return [
            {
                "bill": bill["seq"],
                "task": bill.get("task"),
                "load": bill.get("load"),
                "signed_by": bill.get("device"),
                "readings": [
                    {
                        "seq": c.reading.seq,
                        "device": "scale",
                        "boot": c.reading.boot,
                        "mode": c.reading.mode,
                        "step_g": fmt(c.reading.step) if c.reading.step is not None else None,
                        "shown_g": fmt(c.reading.shown),
                        "tare_base_g": fmt(c.reading.base),
                        "tag_seq": c.container.seq if c.container else None,
                        "containers": c.container.tree if c.container else None,
                    }
                    for c in checks
                ],
            }
        ]

    def _settle_transfers(self, task: str, transfers: List[Dict[str, Any]]) -> Result:
        result = Result(task=task, load=transfers[0].get("load"), status=CLEAN)
        total = Fraction(0)
        lines: List[Dict[str, Any]] = []
        for transfer in transfers:
            load = transfer.get("load")
            before_seq = self._previous_observe(transfer["seq"], load)
            after_seq = self._next_observe(transfer["seq"], load)
            if before_seq is None or after_seq is None:
                result.status = HOLD
                result.problems.append(
                    f"seq {transfer['seq']} 的交接前后缺称量，须补称"
                )
                return result
            before, after = self.check(before_seq), self.check(after_seq)
            basis = same_basis(before, after)
            if basis:
                result.status = BROKEN
                result.problems.extend(basis)
                result.problems.append(
                    f"seq {transfer['seq']} 交接前后不属同一实物基准，须对 seq {after_seq} 补称"
                )
                return result
            for item in (before, after):
                result.problems.extend(item.problems)
                if item.status not in USABLE:
                    result.status = worst(result.status, HOLD)
                elif item.status == NOTICE:
                    result.status = worst(result.status, NOTICE)
            if result.status not in USABLE:
                return result
            if before.low is None or after.high is None:
                result.status = HOLD
                result.problems.append("缺少净重，无法按差量办理")
                return result
            delta_low = before.low - after.high
            delta_high = before.high - after.low
            if delta_high <= 0:
                result.status = HOLD
                result.problems.append("差值不为正")
                return result
            total += delta_low
            lines.append({
                "transfer": transfer["seq"],
                "before": before_seq,
                "after": after_seq,
                "net_g": fmt(round_half_up(delta_low, self.ledger.legal_step)),
                "net_raw_g": fmt(delta_low),
                "net_interval_raw_g": [fmt(delta_low), fmt(delta_high)],
            })
        result.record = {
            "task": task,
            "load": result.load,
            "segments": lines,
            "net_g": fmt(round_half_up(total, self.ledger.legal_step)),
            "net_raw_g": fmt(total),
            "legal_step_g": fmt(self.ledger.legal_step),
            "signers": [
                {
                    "transfer": t["seq"],
                    "device": t.get("device"),
                    "load": t.get("load"),
                    "removed": t.get("removed", []),
                }
                for t in transfers
            ],
        }
        result.notes.append("按交接段落逐段差值累计，外对净重只舍入一次")
        return result

    # -- 剩余物校验 ------------------------------------------------------ #

    def validate_remaining(self, task: str) -> Result:
        row = self._last(lambda v: v.get("task") == task, self.scene.validations)
        if row is None:
            return Result(task=task, load=None, status=HOLD, problems=[f"任务 {task} 没有剩余物校验记录"])
        return self._validate_row(row)

    def _validate_row(self, row: Dict[str, Any]) -> Result:
        refs = list(row.get("refs", []))
        result = Result(task=row["task"], load=row.get("load"), status=CLEAN)
        if not refs:
            result.status = HOLD
            result.problems.append("剩余物校验未引用任何称量")
            return result
        previous_bill = self._previous_bill(row["seq"], row.get("load"))
        if previous_bill is None:
            result.status = HOLD
            result.problems.append("没有已结单可对照，剩余物无从核验")
            return result
        old_seq = previous_bill.get("refs", [None, None])[1]
        observes = [s for s in refs if s in self.scene.by_seq and self.scene.by_seq[s]["kind"] == "observe"]
        new_seq = next((s for s in reversed(observes) if s != old_seq), None)
        if old_seq is None or new_seq is None:
            result.status = HOLD
            result.problems.append("剩余物校验须引用旧余量称量与新称量各一次")
            return result

        old = self.check(old_seq)
        new = self.check(new_seq)
        for item in (old, new):
            result.problems.extend(item.problems)
        if old.status not in USABLE or old.low is None:
            result.status = BROKEN
            result.problems.append(f"已结单的余量称量 seq {old_seq} 需要补称")
            return result
        if new.status not in USABLE or new.low is None:
            result.status = BROKEN
            result.problems.append(f"换容器后的新称量 seq {new_seq} 需要补称")
            return result

        old_remaining = old.low  # 已结单后余量（基准明确，视为定值）
        tolerance = self.ledger.legal_step
        fits = new.low - tolerance <= old_remaining <= new.high + tolerance
        # 已结清净重是旧余量的上限；新称量不得凭空多出食品
        over = new.high > old_remaining + tolerance
        # 可继续办理的净重取保守下界，且不越过已结清净重
        continue_net = max(Fraction(0), min(new.low, old_remaining))
        record = {
            "task": row["task"],
            "load": row.get("load"),
            "previous_bill": previous_bill["seq"],
            "old_remaining_g": fmt(old_remaining),
            "new_net_interval_g": [fmt(new.low), fmt(new.high)],
            "remaining_g": fmt(round_half_up(continue_net, self.ledger.legal_step)),
            "remaining_raw_g": fmt(continue_net),
            "legal_step_g": fmt(tolerance),
            "old_containers": old.container.tree if old.container else None,
            "new_containers": new.container.tree if new.container else None,
        }
        result.record = record
        if over:
            result.status = BROKEN
            result.problems.append(
                f"新称量 seq {new_seq} 的净重上限 {fmt(new.high)}g 超过已结清余量 "
                f"{fmt(old_remaining)}g，不得透支已结清净重，须对 seq {new_seq} 补称"
            )
            return result
        if not fits:
            result.status = BROKEN
            result.problems.append(
                f"新旧互证失败：旧余量 {fmt(old_remaining)}g 不在新称量净重区间 "
                f"{fmt(new.low)}~{fmt(new.high)}g（容差 {fmt(tolerance)}g）内，须补称 seq {new_seq}"
            )
            return result
        if old.container and new.container and old.container.tree != new.container.tree:
            result.notes.append(
                "子筐已更换，但新旧容器下的净重互证成立，可继续办理"
            )
        if old.container and new.container and old.container.condition != new.container.condition:
            result.notes.append("容器干湿条件已变化，已按新条件登记皮重核算")
        result.notes.append("剩余物通过新旧互证，未透支已结清净重")
        return result

    # -- 辅助 ------------------------------------------------------------ #

    def _previous_observe(self, seq: int, load: Optional[str]) -> Optional[int]:
        found = None
        for row in self.rows:
            if row["seq"] >= seq:
                break
            if row["kind"] == "observe" and row.get("load") == load:
                found = row["seq"]
        return found

    def _next_observe(self, seq: int, load: Optional[str]) -> Optional[int]:
        for row in self.rows:
            if row["seq"] > seq and row["kind"] == "observe" and row.get("load") == load:
                return row["seq"]
        return None

    def _previous_bill(self, seq: int, load: Optional[str]) -> Optional[Dict[str, Any]]:
        return self._last(
            lambda b: b["seq"] < seq and (load is None or b.get("load") == load),
            self.scene.bills,
        )

    @staticmethod
    def _last(predicate, items: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        found = None
        for item in items:
            if predicate(item):
                found = item
        return found


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def audit(verifier: Verifier) -> Dict[str, Any]:
    tasks: List[str] = []
    for row in verifier.rows:
        if row["kind"] in ("open", "transfer", "bill", "validate_remaining"):
            task = row.get("task")
            if task and task not in tasks:
                tasks.append(task)
    report = {"tasks": {}, "observations": {}}
    for task in tasks:
        report["tasks"][task] = {
            "settle": verifier.settle(task).as_dict(),
            "remaining": (
                verifier.validate_remaining(task).as_dict()
                if any(v.get("task") == task for v in verifier.scene.validations)
                else None
            ),
        }
    for row in verifier.rows:
        if row["kind"] == "observe":
            report["observations"][row["seq"]] = verifier.explain(row["seq"])
    return report


def render(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    for task, entry in report["tasks"].items():
        settle = entry["settle"]
        rec = settle["record"]
        lines.append(f"任务 {task}  结单状态 {settle['status']}")
        if rec:
            if "net_g" in rec:
                lines.append(
                    f"  对外净重 {rec['net_g']}g（核验原值 {rec.get('net_raw_g')}g，"
                    f"法定步长 {rec.get('legal_step_g')}g）"
                )
        for note in settle["notes"]:
            lines.append(f"  · {note}")
        for problem in settle["problems"]:
            lines.append(f"  ! {problem}")
        remaining = entry.get("remaining")
        if remaining:
            rec = remaining["record"]
            lines.append(f"  剩余物校验 {remaining['status']}")
            if rec:
                lines.append(
                    f"    旧余量 {rec['old_remaining_g']}g，新称量净重 "
                    f"{rec['new_net_interval_g'][0]}~{rec['new_net_interval_g'][1]}g，"
                    f"可继续净重 {rec['remaining_g']}g"
                )
            for note in remaining["notes"]:
                lines.append(f"    · {note}")
            for problem in remaining["problems"]:
                lines.append(f"    ! {problem}")
    return "\n".join(lines) if lines else "没有可裁定的交接记录"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="按设备事实裁定交接净重")
    parser.add_argument("--scene", type=Path, help="同格式的观测记录")
    parser.add_argument("--devices", type=Path, help="设备表")
    parser.add_argument("--policy", type=Path, help="现场合同")
    parser.add_argument("--task", help="只核对该任务")
    parser.add_argument("--seq", type=int, action="append", help="解释某条称量，可重复")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    ledger = Ledger.load(args.devices, args.policy)
    rows = load_observations(args.scene) if args.scene else load_observations()
    verifier = Verifier(ledger, rows)

    if args.seq:
        report: Any = {"observations": [verifier.explain(s) for s in args.seq]}
    elif args.task:
        report = {
            "settle": verifier.settle(args.task).as_dict(),
            "remaining": verifier.validate_remaining(args.task).as_dict(),
        }
    else:
        report = audit(verifier)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.seq:
        for item in report["observations"]:
            print(json.dumps(item, ensure_ascii=False))
    elif args.task:
        print(render({"tasks": {args.task: report}}))
    else:
        print(render(report))
        for seq, item in report["observations"].items():
            print(
                f"称量 seq {seq} [{item['status']}] 档 {item['mode']} 步长 {item['step_g']}g "
                f"毛重 {item['gross_g']}g 净重 {item['net_g']}g"
                + ("  旁路接触" if item["side_touch"] else "")
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
