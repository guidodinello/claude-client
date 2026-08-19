from claude_client._manifest import MANIFEST_NAME, ManifestEntry, load, prune_targets, save


def test_load_missing_manifest_returns_empty(tmp_path):
    assert load(tmp_path) == {}


def test_load_corrupt_manifest_returns_empty_and_backs_up_original(tmp_path):
    manifest_path = tmp_path / MANIFEST_NAME
    manifest_path.write_text("not json")

    assert load(tmp_path) == {}
    assert not manifest_path.exists()  # moved aside, not left in place
    assert (tmp_path / (MANIFEST_NAME + ".corrupt")).read_text() == "not json"


def test_save_and_load_round_trip(tmp_path):
    entries = {"doc-1": ManifestEntry(filename="notes.md", updated_at="2024-01-01")}
    save(tmp_path, entries)
    assert load(tmp_path) == entries


def test_save_does_not_leave_a_temp_file_behind(tmp_path):
    save(tmp_path, {"doc-1": ManifestEntry(filename="notes.md", updated_at="")})
    assert not (tmp_path / (MANIFEST_NAME + ".tmp")).exists()
    assert (tmp_path / MANIFEST_NAME).exists()


def test_prune_targets_empty_manifest():
    current = {"doc-1": ManifestEntry(filename="notes.md", updated_at="")}
    assert prune_targets({}, current) == []


def test_prune_targets_uuid_present_and_unchanged_is_kept():
    entry = ManifestEntry(filename="notes.md", updated_at="")
    previous = {"doc-1": entry}
    current = {"doc-1": entry}
    assert prune_targets(previous, current) == []


def test_prune_targets_uuid_absent_from_current_is_pruned():
    previous = {
        "doc-1": ManifestEntry(filename="notes.md", updated_at=""),
        "doc-2": ManifestEntry(filename="other.md", updated_at=""),
    }
    current = {"doc-1": previous["doc-1"]}
    assert prune_targets(previous, current) == [("doc-2", "other.md")]


def test_prune_targets_renamed_uuid_prunes_old_filename():
    """A uuid still present but resolved to a new filename orphans its old one."""
    previous = {"doc-1": ManifestEntry(filename="old-name.md", updated_at="")}
    current = {"doc-1": ManifestEntry(filename="new-name.md", updated_at="")}
    assert prune_targets(previous, current) == [("doc-1", "old-name.md")]


def test_prune_targets_never_deletes_a_filename_claimed_by_another_live_uuid():
    """A deleted doc's old filename must not be pruned if a different uuid reused it
    this run (freed-name reuse) — deleting it would destroy the new doc's file."""
    previous = {"doc-1": ManifestEntry(filename="notes.md", updated_at="")}
    current = {"doc-2": ManifestEntry(filename="notes.md", updated_at="")}
    assert prune_targets(previous, current) == []
