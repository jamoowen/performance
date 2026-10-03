package main

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
)

func TestSeedAndAtomicStockUpdate(t *testing.T) {
	s, err := openStore(filepath.Join(t.TempDir(), "benchmark.sqlite"), 100)
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	first := s.updateResult(1, 1)
	second := s.updateResult(1, 1)
	if first.status != 200 || second.status != 200 {
		t.Fatalf("updates failed: %d %d", first.status, second.status)
	}
	var integrity map[string]int64
	if err := json.Unmarshal(s.integrityResult().body, &integrity); err != nil {
		t.Fatal(err)
	}
	if integrity["rows"] != 100 || integrity["totalRevisions"] != 2 {
		t.Fatalf("unexpected integrity: %#v", integrity)
	}
}

func TestStrictInputParsing(t *testing.T) {
	if _, err := parseID("-1"); err == nil {
		t.Fatal("negative id accepted")
	}
	if _, _, err := parseListQuery("limit=1&limit=2", 100); err == nil {
		t.Fatal("duplicate query accepted")
	}
	if _, result := parseStock("application/json", strings.NewReader(`{"delta":1.5}`)); result.status != 400 {
		t.Fatalf("float delta status=%d", result.status)
	}
}
