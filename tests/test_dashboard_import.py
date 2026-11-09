"""Dashboard HTML -> catalogue, using an invented sample (no real programme data in the tests)."""
from workflow import setup
from workflow.dashboard_import import dashboard_rows, parse_dashboard, rows_to_csv

SAMPLE = """<html><head><title>Sample</title></head><body><script>
// comment
const DOMAINS = {
  1:{name:'Water Systems', color:'#123456'},
  2:{name:"Mobility", color:'#654321'},
};
const ORIGINAL_DATA = [
  { id:1, num:'PS01', track:1, category:'Monitoring', title:'Leak detection in city pipelines',
    hook:'Find leaks early using pressure sensors.', challenges:['Sensors drift', "Data is noisy"],
    impact:['Less water lost','Lower repair cost','Safer roads'], mapping:'SDG 6', },
  { id:2, num:'PS02', track:2, category:'Transit', title:'Bus arrival prediction',
    hook:'Predict bus arrivals for commuters.', challenges:['Traffic varies'], impact:['Shorter waits'], mapping:'SDG 11' },
];
function render(){ const x = `${ORIGINAL_DATA.length}`; }
</script></body></html>"""


def test_parse_and_convert():
    domains, items = parse_dashboard(SAMPLE)
    assert domains["1"]["name"] == "Water Systems" and len(items) == 2 and items[0]["challenges"] == ["Sensors drift", "Data is noisy"]
    rows = dashboard_rows(SAMPLE)
    assert [r["ps_id"] for r in rows] == ["PS01", "PS02"] and rows[0]["track_id"] == "T1" and rows[1]["track_name"] == "Mobility"
    assert "Challenges: Sensors drift; Data is noisy" in rows[0]["ps_description"] and "Expected impact:" in rows[0]["ps_description"]
    kws = rows[0]["keywords"].split("; ")
    assert "leak" in kws or "detection" in kws and "pipelines" in kws
    ok, errs = setup.parse_catalogue("c.csv", rows_to_csv(rows))
    assert errs == [] and len(ok) == 2


def test_import_into_portal(env):
    s = setup.import_catalogue(env["conn"], env["admin"], "c.csv", rows_to_csv(dashboard_rows(SAMPLE)), True)
    assert s["tracks"] == 2 and s["problem_statements"] == 2


def test_missing_data_reported():
    import pytest

    with pytest.raises(ValueError, match="DOMAINS"):
        parse_dashboard("<html>no data here</html>")
