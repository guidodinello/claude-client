from claude_client._manifest import ManifestEntry, load, prune_targets, save


def test_load_missing_manifest_returns_empty(tmp_path):
    assert load(tmp_path) == {}


def test_load_corrupt_manifest_returns_empty(tmp_path):
    (tmp_path / ".claude-pull-manifest.json").write_text("not json")
    assert load(tmp_path) == {}


def test_save_and_load_round_trip(tmp_path):
    entries = {"doc-1": ManifestEntry(filename="notes.md", updated_at="2024-01-01")}
    save(tmp_path, entries)
    assert load(tmp_path) == entries


def test_prune_targets_empty_manifest():
    assert prune_targets({}, {"doc-1"}) == []


def test_prune_targets_uuid_present_remotely_is_kept():
    previous = {"doc-1": ManifestEntry(filename="notes.md", updated_at="")}
    assert prune_targets(previous, {"doc-1"}) == []


def test_prune_targets_uuid_absent_remotely_is_pruned():
    previous = {
        "doc-1": ManifestEntry(filename="notes.md", updated_at=""),
        "doc-2": ManifestEntry(filename="other.md", updated_at=""),
    }
    assert prune_targets(previous, {"doc-1"}) == ["other.md"]
