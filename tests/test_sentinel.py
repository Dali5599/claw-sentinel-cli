import pytest
import json
from sentinel._metrics import collect_snapshot, collect_host, collect_platform, collect_uptime
from sentinel._alerts import AlertLevel, check_snapshot, ThresholdRule
from sentinel import main

def test_collect_host_and_platform():
    host = collect_host()
    assert host.hostname != ""
    assert host.machine != ""
    assert host.os_name != ""
    plat = collect_platform()
    assert plat.machine != ""
    assert plat.kernel != ""

def test_collect_uptime():
    up = collect_uptime()
    assert up.seconds > 0
    assert up.formatted != ""

def test_collect_snapshot():
    snap = collect_snapshot(tags={"cpu", "memory"}, poll_interval=0.1)
    d = snap.to_dict()
    assert "cpu" in d
    assert "memory" in d
    assert "host" in d
    assert isinstance(d["cpu"]["percent"], (int, float))
    assert isinstance(d["memory"]["percent"], (int, float))

def test_rule_engine_evaluation():
    rules = [
        ThresholdRule(metric="cpu.percent", operator="gt", value=100.0, level=AlertLevel.CRITICAL),
        ThresholdRule(metric="memory.percent", operator="gte", value=0.0, level=AlertLevel.WARNING),
    ]
    snap = collect_snapshot(tags={"cpu", "memory"}, poll_interval=0.1)
    alerts = check_snapshot(snap, rules)
    # Memory percent is >= 0, so it must trigger
    assert any(a.metric == "memory.percent" for a in alerts)
    # CPU percent is <= 100, so critical threshold > 100 will not trigger
    assert not any(a.metric == "cpu.percent" for a in alerts)

def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "0.1.0" in captured.out

def test_cli_check_json(capsys):
    ret = main(["check", "--tags", "cpu,memory", "--format", "json"])
    assert ret == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "cpu" in data
    assert "memory" in data

def test_cli_check_text(capsys):
    ret = main(["check", "--tags", "cpu,memory", "--format", "text"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "claw-sentinel" in captured.out
    assert "cpu" in captured.out
