package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestCatalogUsesTheDocumentedDeterministicFormula(t *testing.T) {
	product := productFor(3)
	if product.Name != "Product 00003" || product.Category != "home" || product.PriceCents != 24257 || product.Stock != 111 {
		t.Fatalf("unexpected product: %+v", product)
	}
}

func TestStorePersistsSeededProductsAndRejectsDifferentSeedCount(t *testing.T) {
	databasePath := filepath.Join(t.TempDir(), "benchmark.sqlite")
	store, err := openStore(databasePath, 20, 2)
	if err != nil {
		t.Fatal(err)
	}
	product, found, err := store.product(context.Background(), 3)
	if err != nil || !found || product.Name != "Product 00003" {
		t.Fatalf("unexpected stored product: %+v, found=%t, err=%v", product, found, err)
	}
	if err := store.close(); err != nil {
		t.Fatal(err)
	}
	if _, err := openStore(databasePath, 21, 1); err == nil {
		t.Fatal("expected seed count mismatch")
	}
}

func TestEveryConnectionGetsRequiredPragmas(t *testing.T) {
	databasePath := filepath.Join(t.TempDir(), "benchmark.sqlite")
	store, err := openStore(databasePath, 20, 2)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = store.close() }()
	context := context.Background()
	for index := 0; index < 2; index++ {
		connection, err := store.db.Conn(context)
		if err != nil {
			t.Fatal(err)
		}
		var tempStore, checkpoint string
		if err := connection.QueryRowContext(context, "PRAGMA temp_store").Scan(&tempStore); err != nil {
			t.Fatal(err)
		}
		if err := connection.QueryRowContext(context, "PRAGMA wal_autocheckpoint").Scan(&checkpoint); err != nil {
			t.Fatal(err)
		}
		if tempStore != "2" || checkpoint != "1000" {
			t.Fatalf("connection pragma mismatch: temp_store=%s checkpoint=%s", tempStore, checkpoint)
		}
		defer func() { _ = connection.Close() }()
	}
	if _, err := os.Stat(databasePath); err != nil {
		t.Fatal(err)
	}
}
