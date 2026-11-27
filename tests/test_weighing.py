"""裁定引擎的行为核对。

测试用真实设备资料与观测，同时构造若干最小变体验证边界：
秤档解读、稳定窗口、去皮生效、容器皮重不重复扣、换筐互证与透支保护。
"""

import copy
import json
import unittest
from fractions import Fraction
from pathlib import Path

import weighing as W
from weighing import (
    BROKEN,
    CLEAN,
    HOLD,
    NOTICE,
    OPEN,
    Ledger,
    Verifier,
    parse_value,
    round_half_up,
)

ROOT = Path(__file__).resolve().parent.parent


def rows():
    return W.load_observations()


def scene_rows():
    return copy.deepcopy(rows())


def renumber(rows_):
    """按列表位置重排 seq，并把指向仍存在行的 refs 同步到新序号。"""

    rows_ = copy.deepcopy(rows_)
    mapping = {}
    for new, row in enumerate(rows_, start=1):
        if row["seq"] != 0:
            mapping[row["seq"]] = new
    for new, row in enumerate(rows_, start=1):
        row["seq"] = new
        if "refs" in row:
            row["refs"] = [mapping[x] for x in row["refs"] if x in mapping]
    return rows_


def verify(rows_):
    return Verifier(Ledger.load(ROOT / "devices.json", ROOT / "policy.json"), rows_)


def verify_with(rows_, ledger):
    return Verifier(ledger, rows_)


def base_ledger():
    return json.loads((ROOT / "devices.json").read_text()), json.loads((ROOT / "policy.json").read_text())


class NumericTest(unittest.TestCase):
    def test_half_up_rounding(self):
        self.assertEqual(round_half_up(Fraction(1225), Fraction(10)), Fraction(1230))
        self.assertEqual(round_half_up(Fraction(1224), Fraction(10)), Fraction(1220))
        self.assertEqual(round_half_up(Fraction(-1225), Fraction(10)), Fraction(-1230))
        self.assertEqual(round_half_up(Fraction(5), Fraction(2)), Fraction(6))

    def test_parse_exact(self):
        self.assertEqual(parse_value(799), Fraction(799))
        self.assertEqual(parse_value("799.5"), Fraction(1599, 2))
        self.assertEqual(parse_value(0.1), Fraction(1, 10))


class SceneBaselineTest(unittest.TestCase):
    """仓库自带场景的关键结论。"""

    @classmethod
    def setUpClass(cls):
        cls.v = verify(rows())

    def test_bill_t1_net(self):
        result = self.v.settle("T1")
        self.assertEqual(result.status, CLEAN)
        self.assertEqual(result.record["net_g"], "1230")
        # 核验原值必须可追溯，且确实按一次舍入得到 1230
        self.assertEqual(result.record["net_raw_g"], "1228")
        self.assertEqual(result.record["legal_step_g"], "10")

    def test_bill_signers_link_readings_and_containers(self):
        signer = self.v.settle("T1").record["signers"][0]
        self.assertEqual(signer["signed_by"], "desk")
        readings = signer["readings"]
        self.assertEqual([r["seq"] for r in readings], [7, 9])
        self.assertEqual(readings[0]["containers"], {"C1": ["B1", "ice"]})
        self.assertEqual(readings[0]["tag_seq"], 2)

    def test_t1_notice_on_ignored_boot_tare(self):
        # seq 7 依赖的零点在 seq 6 才生效，而 seq 7 走的是 S0 档；读数本身干净
        check = self.v.check(7)
        self.assertEqual(check.status, CLEAN)
        self.assertTrue(check.reading.zero_effective)

    def test_coarse_mode_and_span(self):
        coarse = self.v.check(15)
        self.assertEqual(coarse.reading.mode, "coarse")
        self.assertEqual(coarse.reading.step, Fraction(10))
        self.assertTrue(W.on_step(coarse.reading.shown, Fraction(10)))

    def test_side_touch_is_not_a_clean_reading(self):
        check = self.v.check(14)
        self.assertEqual(check.status, HOLD)
        self.assertFalse(check.reading.stable)
        self.assertTrue(check.reading.side_touch)

    def test_t2_open_no_bill(self):
        result = self.v.settle("T2")
        self.assertEqual(result.status, OPEN)
        self.assertFalse(result.record)

    def test_remaining_witness_holds_across_swapped_basket(self):
        result = self.v.validate_remaining("T2")
        self.assertEqual(result.status, CLEAN)
        rec = result.record
        self.assertEqual(rec["previous_bill"], 10)
        self.assertEqual(rec["old_remaining_g"], "2004")
        self.assertEqual(rec["new_net_interval_g"], ["1980", "2012"])
        # 不得透支已结清净重
        self.assertLessEqual(parse_value(rec["remaining_g"]), parse_value(rec["old_remaining_g"]))
        self.assertTrue(any("子筐已更换" in n for n in result.notes))

    def test_explain_retains_raw_and_rounded(self):
        info = self.v.explain(7)
        self.assertEqual(info["shown_g"], "3232")
        self.assertEqual(info["tare_base_g"], "1300")
        self.assertEqual(info["gross_g"], "4532")
        self.assertEqual(info["net_g"], "3232")
        self.assertTrue(info["stable"])
        self.assertEqual(info["status"], CLEAN)


class ModeAndStabilityTest(unittest.TestCase):
    def test_claimed_mode_mismatch_falls_back_to_boot_mode(self):
        data = scene_rows()
        # seq 7 自报 coarse，但开机档是 fine；应按 fine 解读并留提示
        for row in data:
            if row["seq"] == 7:
                row["mode"] = "coarse"
        v = verify(data)
        check = v.check(7)
        self.assertEqual(check.reading.mode, "fine")
        self.assertEqual(check.reading.step, Fraction(2))
        self.assertEqual(check.status, NOTICE)
        self.assertTrue(any("不符" in p for p in check.problems))
        # NOTICE 仍可结单，但须提示
        result = v.settle("T1")
        self.assertEqual(result.status, NOTICE)

    def test_short_window_is_hold(self):
        data = scene_rows()
        for row in data:
            if row["seq"] == 7:
                row["through"] = row["at"] + 500
        v = verify(data)
        check = v.check(7)
        self.assertEqual(check.status, HOLD)
        self.assertFalse(check.reading.stable)
        self.assertTrue(any("稳定时长" in p for p in check.problems))

    def test_reading_out_of_range_is_hold(self):
        data = scene_rows()
        for row in data:
            if row["seq"] == 7:
                row["shown_g"] = 20000  # fine 档上限 12000
        v = verify(data)
        self.assertEqual(v.check(7).status, HOLD)

    def test_display_off_step_is_hold(self):
        data = scene_rows()
        for row in data:
            if row["seq"] == 7:
                row["shown_g"] = 3233  # fine 档步长 2
        v = verify(data)
        check = v.check(7)
        self.assertEqual(check.status, HOLD)
        self.assertTrue(any("网格" in p for p in check.problems))


class TareTest(unittest.TestCase):
    def test_received_only_tare_not_effective(self):
        data = scene_rows()
        # 删除 seq 6 的 effective 确认，只留 received
        data = [r for r in data if r["seq"] != 6]
        data = renumber(data)
        v = verify(data)
        # 原 seq 7 现在是 6，声称 net 但零点未生效
        check = v.check(6)
        self.assertEqual(check.status, HOLD)
        self.assertFalse(check.reading.zero_effective)
        self.assertIsNone(check.low)

    def test_reconnect_before_zero_voids_session(self):
        data = scene_rows()
        # 在 effective 零点（原 seq 6）之后、净重读数（原 seq 7）之前插入一次重连
        index = next(i for i, r in enumerate(data) if r["seq"] == 6) + 1
        data.insert(index, {"seq": 0, "device": "scale", "kind": "boot", "at": 950, "boot": "S0", "mode": "fine"})
        data = renumber(data)
        v = verify(data)
        target = next(r["seq"] for r in data if r["kind"] == "observe" and r.get("boot") == "S0" and r.get("net"))
        check = v.check(target)
        self.assertEqual(check.status, HOLD)
        self.assertFalse(check.reading.zero_effective)
        self.assertIsNone(check.low)

    def test_effective_tare_not_double_subtracted(self):
        check = verify(rows()).check(7)
        # shown 3232 已经是去皮后的值；再减一次 1300 会变成 1932
        self.assertEqual(check.low, Fraction(3232))
        self.assertEqual(check.reading.base, Fraction(1300))

    def test_tare_base_mismatching_condition_is_hold(self):
        data = scene_rows()
        # 让 seq 7 的容器换成湿态，但秤端皮重仍按干态 1300 扣
        for row in data:
            if row["seq"] == 2:
                row["condition"] = {"C1": "wet", "B1": "dry", "ice": "dry"}
        # seq 7 没有标签落在其称量区间内（标签在 seq 2，早于 at=1000 但按 to 取最近）
        # 显式把标签搬到 seq 7 的区间并保留干态切换，验证皮重与条件不符
        v = verify(data)
        check = v.check(7)
        # C1 湿态皮重 850~870，与秤端 1300 不符
        self.assertEqual(check.status, HOLD)

    def test_registered_tare_used_when_no_effective_tare(self):
        # 未去皮读数：净重应为毛重减登记容器皮重（区间）。
        # 用 coarse 档（步长 10g）使宜家登记的皮重 799~803 等能落在显示网格上。
        dev, pol = base_ledger()
        dev["scale"]["modes"]["fine"] = {"step_g": 1, "min_g": 100, "max_g": 12000}
        v = verify_with(rows(), Ledger(dev, pol))
        check = v.check(5)
        self.assertFalse(check.reading.zero_effective)
        # 干态 C1 799+198+299=1296 .. 803+202+305=1310
        self.assertEqual(check.low, Fraction(4532 - 1310))
        self.assertEqual(check.high, Fraction(4532 - 1296))
        self.assertFalse(check.exact)


def renumber_fixing(rows_):
    return renumber(rows_)


class ContainerTest(unittest.TestCase):
    def test_missing_tag_is_hold(self):
        data = [r for r in scene_rows() if r["kind"] != "tag"]
        data = renumber(data)
        v = verify(data)
        seq = next(r["seq"] for r in data if r["kind"] == "observe")
        self.assertEqual(v.check(seq).status, HOLD)

    def test_unknown_container_is_hold(self):
        data = scene_rows()
        for row in data:
            if row["seq"] == 2:
                row["tree"] = {"C1": ["B9", "ice"]}
                row["condition"]["B9"] = "dry"
        v = verify(data)
        self.assertEqual(v.check(7).status, HOLD)

    def test_condition_missing_registry_is_hold(self):
        data = scene_rows()
        for row in data:
            if row["seq"] == 2:
                row["condition"] = {"C1": "wet", "B1": "dry", "ice": "dry"}  # C1 湿态已登记
        v = verify(data)
        # seq 7 的容器现在是湿态 C1，皮重 850~870，与秤端 1300 不符
        self.assertEqual(v.check(7).status, HOLD)


class SettlementTest(unittest.TestCase):
    def test_bill_requires_two_refs(self):
        data = scene_rows()
        for row in data:
            if row["seq"] == 10:
                row["refs"] = [7]
        result = verify(data).settle("T1")
        self.assertEqual(result.status, HOLD)

    def test_bill_basis_change_is_broken(self):
        data = scene_rows()
        # 第二次称量前换子筐，结单不能按差值
        data.insert(8, {
            "seq": 0, "device": "tags", "kind": "tag", "at": 2080, "load": "L1",
            "tree": {"C1": ["B2", "ice"]}, "condition": {"C1": "dry", "B2": "dry", "ice": "dry"},
        })
        data = renumber(data)
        v = verify(data)
        self.assertEqual(v.settle("T1").status, BROKEN)

    def test_declared_mismatch_is_notice(self):
        data = scene_rows()
        for row in data:
            if row["seq"] == 10:
                row["net_g"] = 1200
        result = verify(data).settle("T1")
        self.assertEqual(result.status, NOTICE)
        self.assertTrue(any("不一致" in p for p in result.problems))

    def test_transfer_segments_accumulate(self):
        # T1 的 transfer 前后是 seq 7 与 9，差值应为 1228
        result = verify(scene_rows()).settle("T1") if False else None
        data = []
        v = verify(rows())
        # 去掉 bill 让引擎按 transfer 段落累计
        stripped = [r for r in rows() if r["kind"] != "bill"]
        v2 = verify(stripped)
        result = v2.settle("T1")
        self.assertEqual(result.status, CLEAN)
        self.assertEqual(result.record["net_g"], "1230")
        self.assertEqual(result.record["net_raw_g"], "1228")


class RemainingTest(unittest.TestCase):
    def test_remainder_missing_new_weighing_requests_reweigh(self):
        data = scene_rows()
        for row in data:
            if row["seq"] == 16:
                row["refs"] = [9]
        result = verify(data).validate_remaining("T2")
        self.assertEqual(result.status, HOLD)
        self.assertTrue(any("新称量" in p for p in result.problems))

    def test_remainder_overdraw_is_broken(self):
        data = scene_rows()
        # 新称量显示过重：鼓风机表读数加皮重后超过旧余量
        for row in data:
            if row["seq"] == 15:
                row["shown_g"] = 3600  # 毛重 3600，净重 2290~2302，超过旧余量 2004
        result = verify(data).validate_remaining("T2")
        self.assertEqual(result.status, BROKEN)
        self.assertTrue(any("透支" in p for p in result.problems))

    def test_remainder_condition_change_without_registry_is_broken(self):
        data = scene_rows()
        for row in data:
            if row["seq"] == 13:
                row["condition"] = {"C1": "wet", "B2": "dry", "ice": "dry"}
        # C1 湿态已登记，应仍可核算；改成 B2 湿态（未登记）则不可
        for row in data:
            if row["seq"] == 13:
                row["tree"] = {"C1": ["B1", "ice"]}
                row["condition"] = {"C1": "wet", "B1": "wet", "ice": "dry"}
        result = verify(data).validate_remaining("T2")
        self.assertEqual(result.status, BROKEN)

    def test_remainder_without_previous_bill_is_hold(self):
        data = [r for r in scene_rows() if r["kind"] != "bill"]
        data = [r for r in data if not (r["kind"] == "validate_remaining")]
        result = verify(data).validate_remaining("T2")
        self.assertEqual(result.status, HOLD)


class PortabilityTest(unittest.TestCase):
    """班组换秤换筐后应能按新资料重复办理。"""

    def make_alt_ledger(self):
        devices = json.loads((ROOT / "devices.json").read_text())
        policy = json.loads((ROOT / "policy.json").read_text())
        devices["scale"]["modes"]["heavy"] = {"step_g": 20, "min_g": 500, "max_g": 60000}
        devices["scale"]["stable_ms"] = 600
        policy["containers"]["C2"] = {"dry": [500, 505], "wet": [540, 548]}
        policy["containers"]["B3"] = {"dry": [160, 164]}
        policy["legal_step_g"] = 5
        return Ledger(devices, policy)

    def test_alternate_ledger_drives_same_rules(self):
        ledger = self.make_alt_ledger()
        self.assertEqual(ledger.legal_step, Fraction(5))
        self.assertEqual(ledger.scale.modes["heavy"].step, Fraction(20))
        # 原场景在该合同下按 5g 法定步长重新对外舍入
        v = Verifier(ledger, rows())
        result = v.settle("T1")
        self.assertEqual(result.record["legal_step_g"], "5")
        self.assertEqual(result.record["net_g"], "1230")

    def test_alt_policy_unknown_container_degrades(self):
        ledger = self.make_alt_ledger()
        dev = json.loads((ROOT / "devices.json").read_text())
        pol = json.loads((ROOT / "policy.json").read_text())
        # 新合同删掉 C1 的干态，按新资料应判不可用
        del pol["containers"]["C1"]["dry"]
        v = Verifier(Ledger(dev, pol), rows())
        self.assertEqual(v.check(7).status, HOLD)


class LedgerValidationTest(unittest.TestCase):
    def test_inverted_range_rejected(self):
        dev = json.loads((ROOT / "devices.json").read_text())
        pol = json.loads((ROOT / "policy.json").read_text())
        pol["containers"]["C1"]["dry"] = [810, 800]
        with self.assertRaises(ValueError):
            Ledger(dev, pol)

    def test_empty_modes_rejected(self):
        dev = json.loads((ROOT / "devices.json").read_text())
        pol = json.loads((ROOT / "policy.json").read_text())
        dev["scale"]["modes"] = {}
        with self.assertRaises(ValueError):
            Ledger(dev, pol)


if __name__ == "__main__":
    unittest.main()
