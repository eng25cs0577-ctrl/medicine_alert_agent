"""Reading real CDSCO PDFs (June 2025 state + central lists)."""
import pytest

from conftest import CENTRAL_PDF, STATE_PDF
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
