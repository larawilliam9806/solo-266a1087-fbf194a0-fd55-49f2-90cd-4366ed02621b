"""端到端业务规则测试。"""

from __future__ import annotations

from tests.conftest import csv_bytes, facts_by_flight, upload

PLAN = """航班号,目的地,计划到达UTC分钟,状态
CA101,PEK,600,正常
CA102,PVG,660,正常
CA103,CAN,720,正常
CA104,SZX,780,正常
CA105,CTU,840,取消
CA106,HGH,900,取消
CA107,KMG,960,正常
"""

LAND_D0 = """计划起飞日,航班号,实际机场,实际到达UTC分钟
2026-11-18,CA101,PEK,590
2026-11-18,CA102,PVG,700
2026-11-18,CA103,SZX,730
2026-11-18,CA106,HGH,910
2026-11-18,CA999,PEK,1000
"""

LAND_D1 = """计划起飞日,航班号,实际机场,实际到达UTC分钟
2026-11-18,CA104,SZX,1500
2026-11-18,CA107,KMG,1470
2026-11-18,CA102,PVG,1520
"""

D = "2026-11-18"
D1 = "2026-11-19"


def _seed_all(client):
    assert upload(client, "plans", D, 1, csv_bytes(PLAN)).status_code == 200
    assert upload(client, "landings", D, 1, csv_bytes(LAND_D0)).status_code == 200
    assert upload(client, "landings", D1, 1, csv_bytes(LAND_D1)).status_code == 200


def test_incomplete_until_all_three_lists_present(client):
    # 只有计划：不得产出任何事实，且不能判定取消/到达。
    upload(client, "plans", D, 1, csv_bytes(PLAN))
    rep = client.get(f"/api/reports/{D}").json()
    assert rep["status"] == "incomplete"
    assert rep["facts"] == []
    assert f"landing:{D}" in rep["missing_sources"]
    assert f"landing:{D1}" in rep["missing_sources"]

    upload(client, "landings", D, 1, csv_bytes(LAND_D0))
    rep = client.get(f"/api/reports/{D}").json()
    assert rep["status"] == "incomplete"
    assert rep["facts"] == []
    assert rep["missing_sources"] == [f"landing:{D1}"]


def test_full_rule_matrix(client):
    _seed_all(client)
    rep = client.get(f"/api/reports/{D}").json()
    assert rep["status"] == "complete"
    assert rep["source_versions"] == {
        "plan": 1,
        f"landing_{D}": 1,
        f"landing_{D1}": 1,
    }
    f = facts_by_flight(rep)

    # 提前到达记零延误。
    assert f["CA101"]["fact_type"] == "arrived"
    assert f["CA101"]["delay_min"] == 0
    assert f["CA101"]["actual_airport"] == "PEK"

    # 同目的地正常延误（700-660=40），同航班次日又落地 -> 多次落地待核优先。
    assert f["CA102"]["fact_type"] == "review"
    assert "MULTIPLE_LANDINGS" in f["CA102"]["review_reasons"]
    assert f["CA102"]["landing_count"] == 2

    # 备降：记实际机场，不计算原目的地延误。
    assert f["CA103"]["fact_type"] == "diverted"
    assert f["CA103"]["actual_airport"] == "SZX"
    assert f["CA103"]["delay_min"] is None

    # 跨午夜落地：1500-780=720 分钟延误。
    assert f["CA104"]["fact_type"] == "arrived"
    assert f["CA104"]["delay_min"] == 720

    # 取消且未落地 -> 取消。
    assert f["CA105"]["fact_type"] == "cancelled"
    assert f["CA105"]["actual_airport"] is None

    # 取消却落地 -> 待核。
    assert f["CA106"]["fact_type"] == "review"
    assert f["CA106"]["review_reasons"] == ["CANCELLED_BUT_LANDED"]

    # 正常跨午夜延误：1470-960=510。
    assert f["CA107"]["fact_type"] == "arrived"
    assert f["CA107"]["delay_min"] == 510

    # 孤儿落地：计划清单中无此航班。
    assert f["CA999"]["fact_type"] == "review"
    assert f["CA999"]["review_reasons"] == ["ORPHAN_LANDING"]
    assert f["CA999"]["sched_airport"] is None

    # 每条事实都携带所用源版号。
    for fact in rep["facts"]:
        assert fact["source_versions"] == {"plan": 1, "landing_d0": 1, "landing_d1": 1}


def test_normal_missing_landing_is_review_not_arrived(client):
    plan = (
        "航班号,目的地,计划到达UTC分钟,状态\n"
        "CA201,PEK,600,正常\n"
        "CA202,PEK,610,取消\n"
    )
    empty_d0 = "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
    empty_d1 = "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
    upload(client, "plans", D, 1, csv_bytes(plan))
    upload(client, "landings", D, 1, csv_bytes(empty_d0))
    upload(client, "landings", D1, 1, csv_bytes(empty_d1))
    f = facts_by_flight(client.get(f"/api/reports/{D}").json())
    assert f["CA201"]["fact_type"] == "review"
    assert f["CA201"]["review_reasons"] == ["NORMAL_MISSING_LANDING"]
    assert f["CA202"]["fact_type"] == "cancelled"


def test_landing_revision_recomputes_two_plan_dates_together(client):
    # 相邻两个计划日各有计划，落地日 D1 的文件同时影响 D 与 D1。
    plan_d = "航班号,目的地,计划到达UTC分钟,状态\nCA101,PEK,600,正常\n"
    plan_d1 = "航班号,目的地,计划到达UTC分钟,状态\nCA301,PEK,700,正常\n"
    upload(client, "plans", D, 1, csv_bytes(plan_d))
    upload(client, "plans", D1, 1, csv_bytes(plan_d1))
    upload(client, "landings", D, 1, csv_bytes(
        "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
        f"{D},CA101,PEK,620\n"
    ))
    # 首版 D1 落地文件：CA101 缺次日记录不影响（它已落地），CA301 缺落地 -> 待核。
    upload(client, "landings", D1, 1, csv_bytes(
        "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
    ))
    # D1 计划日的结论还依赖 D2 落地清单。
    upload(client, "landings", "2026-11-20", 1, csv_bytes(
        "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
    ))
    f_d = facts_by_flight(client.get(f"/api/reports/{D}").json())
    assert f_d["CA101"]["delay_min"] == 20
    f_d1 = facts_by_flight(client.get(f"/api/reports/{D1}").json())
    assert f_d1["CA301"]["review_reasons"] == ["NORMAL_MISSING_LANDING"]

    # 修订 D1 落地文件（版号 2）：补 CA301，并给 CA101 增加一次次日落地。
    resp = upload(client, "landings", D1, 2, csv_bytes(
        "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
        f"{D},CA101,PEK,1500\n"
        f"{D1},CA301,PEK,715\n"
    ))
    assert resp.json()["recomputed_dates"] == [D, D1]

    f_d = facts_by_flight(client.get(f"/api/reports/{D}").json())
    assert f_d["CA101"]["fact_type"] == "review"
    assert f_d["CA101"]["landing_count"] == 2
    # 结论版号同步切到新版，杜绝混版。
    assert f_d["CA101"]["source_versions"]["landing_d1"] == 2

    f_d1 = facts_by_flight(client.get(f"/api/reports/{D1}").json())
    assert f_d1["CA301"]["fact_type"] == "arrived"
    assert f_d1["CA301"]["delay_min"] == 15
    # 对计划日 D1：D1 落地文件是 landing_d0（已升到 2），D2 文件仍是 landing_d1=1。
    assert f_d1["CA301"]["source_versions"]["landing_d0"] == 2
    assert f_d1["CA301"]["source_versions"]["landing_d1"] == 1
    # 而对计划日 D：同一 D1 文件是 landing_d1，也同步升到 2。
    assert f_d["CA101"]["source_versions"]["landing_d1"] == 2


def test_plan_revision_replaces_whole_file(client):
    _seed_all(client)
    # 修订计划：CA105 改为正常；整文件替换，旧版 CA107 等行不再存在。
    new_plan = """航班号,目的地,计划到达UTC分钟,状态
CA101,PEK,600,正常
CA102,PVG,660,正常
CA103,CAN,720,正常
CA104,SZX,780,正常
CA105,CTU,840,正常
"""
    upload(client, "plans", D, 2, csv_bytes(new_plan))
    f = facts_by_flight(client.get(f"/api/reports/{D}").json())
    # CA106/CA107 已从计划清单移除，但其落地报文仍在 -> 转为孤儿落地待核，
    # 而不是悄悄消失（删除计划不会删除落地，修订由后续新版落地文件完成）。
    assert f["CA106"]["fact_type"] == "review"
    assert f["CA106"]["review_reasons"] == ["ORPHAN_LANDING"]
    assert f["CA107"]["review_reasons"] == ["ORPHAN_LANDING"]
    # CA105 现在正常但没有落地 -> 待核。
    assert f["CA105"]["review_reasons"] == ["NORMAL_MISSING_LANDING"]
    assert f["CA101"]["source_versions"]["plan"] == 2

    # 随后用新版落地清单清空，孤儿与缺落地结论随之改变。
    upload(client, "landings", D, 2, csv_bytes(
        "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
    ))
    upload(client, "landings", D1, 2, csv_bytes(
        "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
    ))
    f = facts_by_flight(client.get(f"/api/reports/{D}").json())
    # 落地全部清空后：孤儿消失，剩余计划航班各按取消/缺落地下结论。
    assert "CA106" not in f and "CA107" not in f and "CA999" not in f
    assert f["CA105"]["review_reasons"] == ["NORMAL_MISSING_LANDING"]
    assert f["CA101"]["source_versions"]["landing_d0"] == 2


def test_version_must_be_strictly_greater(client):
    upload(client, "plans", D, 2, csv_bytes(PLAN))
    r = upload(client, "plans", D, 2, csv_bytes(PLAN))
    assert r.status_code == 409
    r = upload(client, "plans", D, 1, csv_bytes(PLAN))
    assert r.status_code == 409
    # 旧版被拒后数据仍是版号 2。
    assert client.get("/api/sources").json()["plan"][0]["version"] == 2


def test_bad_csv_rejected_without_touching_data(client):
    upload(client, "plans", D, 1, csv_bytes(PLAN))
    bad = "航班号,目的地,计划到达UTC分钟,状态\nCAX,PEK,9999,正常\n"
    r = upload(client, "plans", D, 2, csv_bytes(bad))
    assert r.status_code == 422
    rep = client.get(f"/api/reports/{D}").json()
    # 版号仍为 1，且因缺落地清单没有事实。
    assert rep["source_versions"]["plan"] == 1
    assert rep["facts"] == []

    r2 = upload(client, "landings", D, 1, csv_bytes(
        "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
        f"{D1},CA101,PEK,300\n"  # 计划日与落地日既非同日也非次日关系
    ))
    assert r2.status_code == 422


def test_landing_file_date_must_match_arrival_minute(client):
    # 分钟 1500 属次日，但上传到 D 日期 -> 拒绝。
    r = upload(client, "landings", D, 1, csv_bytes(
        "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
        f"{D},CA101,PEK,1500\n"
    ))
    assert r.status_code == 422
    # 同一行上传到 D1 日期 -> 接受。
    r = upload(client, "landings", D1, 1, csv_bytes(
        "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
        f"{D},CA101,PEK,1500\n"
    ))
    assert r.status_code == 200


def test_landing_date_only_plan_day_or_next(client):
    r = upload(client, "landings", D, 1, csv_bytes(
        "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
        f"{D1},CA101,PEK,600\n"  # 落地日 D 早于计划日 D1
    ))
    assert r.status_code == 422


def test_removing_a_list_invalidates_conclusions(client):
    _seed_all(client)
    assert client.get(f"/api/reports/{D}").json()["status"] == "complete"
    # 用空清单替换 D0 落地文件（同版号不行，须新版号）。
    upload(client, "landings", D, 2, csv_bytes(
        "计划起飞日,航班号,实际机场,实际到达UTC分钟\n"
    ))
    rep = client.get(f"/api/reports/{D}").json()
    # 清单仍“齐全”（版号存在），按空清单重算：取消的仍取消，正常的全部缺落地待核。
    assert rep["status"] == "complete"
    f = facts_by_flight(rep)
    assert f["CA105"]["fact_type"] == "cancelled"
    assert f["CA101"]["review_reasons"] == ["NORMAL_MISSING_LANDING"]
    assert "CA999" not in f  # 孤儿随旧落地清单一起消失


def test_concurrent_uploads_never_mix_versions(tmp_path, monkeypatch):
    import concurrent.futures
    import sqlite3

    monkeypatch.setenv("AIRPORT_DB_PATH", str(tmp_path / "conc.db"))
    from app import db as db_mod
    from app import services

    db_mod.init_db()
    content = (
        "航班号,目的地,计划到达UTC分钟,状态\n"
        "CA101,PEK,600,正常\n"
    ).encode()

    # 五个线程同时以相同版号 1 上传同一日期：锁 + 版号检查保证仅一个成功。
    def one_upload(_):
        from app.csv_parser import parse_plan_csv

        try:
            services.upload_plan(D, 1, parse_plan_csv(content))
            return 200
        except services.VersionConflictError:
            return 409

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        statuses = sorted(pool.map(one_upload, range(5)))
    assert statuses == [200, 409, 409, 409, 409]

    conn = sqlite3.connect(_env_db())
    try:
        ver = conn.execute(
            "SELECT version FROM source_versions WHERE kind='plan'"
        ).fetchone()[0]
        n = conn.execute("SELECT COUNT(*) FROM plan_entries").fetchone()[0]
        dupes = conn.execute(
            "SELECT COUNT(*) FROM (SELECT flight_no, COUNT(*) c FROM plan_entries "
            "GROUP BY flight_no HAVING c > 1)"
        ).fetchone()[0]
    finally:
        conn.close()
    assert ver == 1 and n == 1 and dupes == 0


def test_failure_keeps_old_data_and_conclusions(tmp_path, monkeypatch):
    """事务失败时旧数据与旧结论原样保留（不混版）。"""
    import sqlite3

    monkeypatch.setenv("AIRPORT_DB_PATH", str(tmp_path / "fail.db"))
    from app import db as db_mod
    from app import services
    from app.csv_parser import parse_plan_csv

    db_mod.init_db()
    services.upload_plan(D, 1, parse_plan_csv(csv_bytes(PLAN)))
    services.upload_landing(D, 1, parse_landing_csv_safe(LAND_D0, D))
    services.upload_landing(D1, 1, parse_landing_csv_safe(LAND_D1, D1))
    before = services.get_report(D)
    assert before["status"] == "complete"
    n_before = len(before["facts"])

    # 让重算阶段抛错，模拟上传中途失败：整个事务回滚。
    import app.services as svc

    original = svc.db.upsert_fact
    def boom(*_a, **_kw):
        raise RuntimeError("simulated mid-upload failure")

    svc.db.upsert_fact = boom
    try:
        try:
            services.upload_plan(D, 2, parse_plan_csv(csv_bytes(PLAN)))
            raised = False
        except RuntimeError:
            raised = True
    finally:
        svc.db.upsert_fact = original
    assert raised

    after = services.get_report(D)
    assert after["status"] == "complete"
    assert after["source_versions"]["plan"] == 1  # 旧版号仍在
    assert len(after["facts"]) == n_before

    conn = sqlite3.connect(_env_db())
    try:
        n_plan = conn.execute(
            "SELECT COUNT(*) FROM plan_entries WHERE version=2"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n_plan == 0  # 新版数据未残留


def parse_landing_csv_safe(text: str, file_date: str):
    from app.csv_parser import parse_landing_csv

    return parse_landing_csv(csv_bytes(text), file_date)


def _env_db():
    import os

    return os.environ["AIRPORT_DB_PATH"]


def test_restart_repairs_and_keeps_consistent_versions(client, tmp_path, monkeypatch):
    _seed_all(client)
    # 模拟异常中断：手工把结论版号弄脏，重启自愈应纠正。
    import sqlite3

    db_file = _env_db()
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("UPDATE arrival_facts SET plan_version=999")
    conn.commit()
    conn.close()

    from app import services

    result = services.repair_on_startup(db_file)
    assert D in result["checked_dates"]
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT DISTINCT plan_version FROM arrival_facts").fetchall()
    conn.close()
    assert [r[0] for r in rows] == [1]
