"""Reading real CDSCO PDFs (June 2025 state + central lists)."""
import pytest

from conftest import CENTRAL_PDF, REVISED_PDF, SPURIOUS_PDF, STATE_PDF
from nsq_parser import detect_kind, detect_month, normalize_batch, normalize_date, parse_pdf, parse_rows


def test_state_list_read_completely():
    lst = parse_pdf(STATE_PDF)
    assert lst.kind == "state_nsq" and lst.month == "2025-06"
    assert len(lst.entries) == 130 and [e.sno for e in lst.entries] == list(range(1, 131))
    first = lst.entries[0]
    assert first.drug_name == "Lorazepam Tablets I.P (1 mg)" and first.batch == "3305"
    assert (first.mfg_date, first.exp_date) == ("2024-04", "2028-03")
    assert first.manufacturer.startswith("M/s. Reliance Formulation") and first.reported_by == "Karnataka"
    assert all(e.batch for e in lst.entries)


def test_central_list_read_completely():
    lst = parse_pdf(CENTRAL_PDF)
    assert lst.kind == "central_nsq" and lst.month == "2025-06" and len(lst.entries) == 55
    e = lst.entries[0]
    assert (e.drug_name, e.batch, e.exp_date, e.reported_by) == (
        "Dextrose Injection I.P. 5%w/v (D5)", "1-3098", "2027-12", "CDL, Kolkata")
    assert next(x for x in lst.entries if x.sno == 17).batch == "DXT202412040"   # split over two lines


def test_all_dates_normalised_except_not_mentioned():
    for pdf in (STATE_PDF, CENTRAL_PDF):
        for e in parse_pdf(pdf).entries:
            for d in (e.mfg_date, e.exp_date):
                assert len(d) == 7 or d == "NM", (e.sno, d)


@pytest.mark.parametrize("raw,expected", [
    ("Apr-24", "2024-04"), ("01/2025", "2025-01"), ("12-09-2024", "2024-09"), ("O c t - 2 7", "2027-10"),
    ("11-09- 2026", "2026-09"), ("Sept. 2024", "2024-09"), ("NM", "NM")])
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


def test_detection_and_batch_cleanup():
    assert detect_kind("List of spurious Drugs for the month of June-2025") == "spurious"
    assert detect_month("STATE NSQ ALERT FOR THE MONTH OF June 2025") == "2025-06"
    assert normalize_batch(" t24h459a\n") == "T24H459A"


def test_columns_found_by_header_even_if_reordered():
    rows = [["S.No", "Batch No.", "Product/Drug Name", "Manufactured By", "Expiry Date", "Manufacturing Date",
             "NSQ Result", "Reported by"],
            ["1.", "AB12", "Test Tablets 5 mg", "M/s. X Ltd.", "Jan-27", "Feb-25", "Assay", "Kerala"],
            ["(Note: footnote text)"]]
    [e] = parse_rows(rows)
    assert (e.drug_name, e.batch, e.exp_date, e.mfg_date, e.manufacturer) == (
        "Test Tablets 5 mg", "AB12", "2027-01", "2025-02", "M/s. X Ltd.")


def test_spurious_list_read_completely():
    """Real June 2025 spurious list: merged header cells, names split over table rows,
    a page break, and "Under Investigation" in place of the maker."""
    lst = parse_pdf(SPURIOUS_PDF)
    assert lst.kind == "spurious" and lst.month == "2025-06"
    assert [(e.sno, e.batch) for e in lst.entries] == [
        (1, "24460967"), (2, "I404138"), (3, "SIF2676A"), (4, "SIF2736A")]
    first, second = lst.entries[:2]
    assert first.drug_name == "Cefixime Tablets IP (TAXIM-O 200 TABLETS)"
    assert (first.mfg_date, first.exp_date) == ("2024-07", "2026-06")
    assert first.manufacturer == ""                              # "Under Investigation"
    assert first.result == "Identification and Assay of Cefixime Trihydrate"
    assert first.reported_by == "Drugs Inspector, Bihar"
    assert second.drug_name == "Ointment of Heparin Sodium and Benzyl Nicotinate (THROMBOPHOB)"
    assert lst.entries[3].drug_name == "Rosuvas F 20 Tablets Rosuvastatin and Fenofibrate Tablets IP"
    assert lst.entries[3].exp_date == "2027-05"                  # "05-2027"


def test_revised_drug_alert_read():
    """'Revised' drug alert: the heading names NSQ/spurious/adulterated, but its only row is spurious."""
    lst = parse_pdf(REVISED_PDF)
    assert lst.kind == "spurious" and lst.month == "2025-05"
    [e] = lst.entries
    assert (e.sno, e.drug_name, e.batch) == (19, "Tranexamic Acid Tablets IP (Trenaxa 500)", "18242021A")
    assert (e.mfg_date, e.exp_date, e.manufacturer) == ("2024-08", "2027-07", "")


def test_mixed_drug_alert_kind():
    heading = "Drug Alert: declared as Not of Standard Quality/Spurious/Adulterated/Misbranded"
    assert detect_kind(heading) == "other"
    assert detect_kind(heading, all_spurious=True) == "spurious"


def test_data_row_mentioning_batch_and_drug_is_not_a_header():
    rows = [["S.No.", "Name of Drugs", "Batch No.", "Manufactured By", "Firm's reply"],
            ["1.", "Test Tablets", "AB1", "Under Investigation", "batch not made by them; spurious drug"]]
    [e] = parse_rows(rows)
    assert (e.drug_name, e.batch, e.manufacturer) == ("Test Tablets", "AB1", "")


@pytest.mark.parametrize("text,month", [
    ("NSQ ALERT FOR THE MONTH OF Feb-2025", "2025-02"),          # live CDSCO title, was "?"
    ("State NSQ Alert For The Month April-2025", "2025-04"),
    ("declared as Spurious for the Month of March- 2025", "2025-03"),
    ("Revise list drug alert November-2024", "2024-11"),
    ("for the month of the year 2025", "")])
def test_detect_month_short_names(text, month):
    assert detect_month(text) == month


def test_garbled_titles_repaired():
    from cdsco_page import fix_text, parse_alerts_page
    assert fix_text("Month of Januaryâ\u0080\u0093 2025 Revised") == "Month of January– 2025 Revised"
    assert fix_text("Café – 500 mg") == "Café – 500 mg"           # correct text untouched
    page = ("<table><tr><td>1</td><td>Spurious list for the Month of Januaryâ\u0080\u0093 2025</td>"
            "<td>2025-May-20</td><td><a href='/a.pdf'>p</a></td></tr></table>")
    [link] = parse_alerts_page(page, "https://cdsco.gov.in/")
    assert link.title == "Spurious list for the Month of January– 2025"


def test_stored_lists_repaired(tmp_db):
    import storage
    from models import NSQEntry, NSQList
    e = [NSQEntry(drug_name="X Tablets", batch="B1")]
    storage.save_list(NSQList(title="NSQ ALERT FOR THE MONTH OF Feb-2025", entries=e), "h1")
    storage.save_list(NSQList(title="Spurious, Month of Januaryâ\u0080\u0093 2025", month="2025-01",
                              entries=e), "h2")
    assert storage.repair_lists() == 2
    got = {l["title"]: l["month"] for l in storage.get_lists()}
    assert got == {"NSQ ALERT FOR THE MONTH OF Feb-2025": "2025-02",
                   "Spurious, Month of January– 2025": "2025-01"}
    assert storage.repair_lists() == 0


def test_mixed_list_marks_spurious_rows_only():
    """2023-24 'Drug Alert' lists mix failed-quality and fake rows in one table."""
    from nsq_parser import _parse
    rows = [["S.No.", "Name of Drugs", "Batch No.", "Manufactured By", "Reason for failure", "Remarks"],
            ["1.", "Real Tablets", "A1", "M/s. Good Ltd.", "Dissolution", "Firm has not disputed the result"],
            ["2.", "Fake Tablets", "B2", "Under Investigation", "Identification",
             "The product is purported to be spurious, subject to investigation."],
            ["3.", "Other Tablets", "C3", "M/s. Fine Ltd.", "Assay", "The firm says it is not spurious"]]
    assert [sp for _, sp in _parse(rows)] == [False, True, False]
    assert detect_kind("Drug Alert for the Month of February 2024", any_spurious=True) == "mixed"
