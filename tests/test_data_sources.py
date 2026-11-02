from pathlib import Path

from tracker.data_sources import CsvDataSource


def test_csv_data_source_reads_all_rows() -> None:
    csv_path = Path(__file__).resolve().parents[1] / "data" / "carols_schedule.csv"
    data_source = CsvDataSource(csv_path)
    stops = data_source.get_stops()
    assert len(stops) >= 1


def test_time_window_filtering() -> None:
    csv_path = Path(__file__).resolve().parents[1] / "data" / "carols_schedule.csv"
    data_source = CsvDataSource(csv_path)
    filtered = data_source.get_stops(time_window=("00:00", "23:59"))
    assert filtered