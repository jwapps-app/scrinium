from datetime import date

from app.services.dates import extract_document_date


def test_iso_date():
    assert extract_document_date("Issued 2024-03-05 by the city") == date(2024, 3, 5)


def test_month_name_date():
    assert extract_document_date("Statement date: March 5, 2024") == date(2024, 3, 5)


def test_day_first_month_name():
    assert extract_document_date("Paris, 5 March 2024") == date(2024, 3, 5)


def test_numeric_mdy_default():
    # DATE_ORDER defaults to MDY: 03/04/2024 is March 4th.
    assert extract_document_date("Due 03/04/2024") == date(2024, 3, 4)


def test_first_match_in_reading_order_wins():
    text = "Paid 2024-06-01. Original invoice date 2020-01-15."
    assert extract_document_date(text) == date(2024, 6, 1)


def test_implausible_years_rejected():
    assert extract_document_date("In the year 1802-01-01 nothing happened") is None


def test_far_future_rejected():
    assert extract_document_date("Expires 2199-01-01") is None


def test_no_date():
    assert extract_document_date("no dates here at all") is None


def test_garbage_numeric_not_a_date():
    assert extract_document_date("part number 99/99/2024 is invalid") is None
