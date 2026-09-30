package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/mattn/go-sqlite3"
)

const schemaVersion = "1"

func init() {
	sql.Register("benchmark-sqlite3", &sqlite3.SQLiteDriver{ConnectHook: func(connection *sqlite3.SQLiteConn) error {
		for _, statement := range []string{"PRAGMA temp_store=MEMORY", "PRAGMA wal_autocheckpoint=1000"} {
			if _, err := connection.Exec(statement, nil); err != nil {
				return err
			}
		}
		return nil
	}})
}

type Store struct {
	db            *sql.DB
	detail        *sql.Stmt
	listCount     *sql.Stmt
	listRows      *sql.Stmt
	catalogReport *sql.Stmt
	eventsReport  *sql.Stmt
	eventUpsert   *sql.Stmt
}

func openStore(path string, seedCount, maxOpenConnections int) (*Store, error) {
	if path == ":memory:" {
		return nil, errors.New("DB_PATH must name a filesystem database")
	}
	absPath, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(absPath), 0755); err != nil {
		return nil, err
	}
	uri := url.URL{Scheme: "file", Path: absPath}
	dsn := uri.String() + "?_journal_mode=WAL&_synchronous=NORMAL&_foreign_keys=on&_busy_timeout=5000&_cache_size=-2000&_txlock=immediate"
	db, err := sql.Open("benchmark-sqlite3", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(maxOpenConnections)
	db.SetMaxIdleConns(maxOpenConnections)
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, err
	}
	var journalMode string
	if err := db.QueryRow("PRAGMA journal_mode").Scan(&journalMode); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("read WAL mode: %w", err)
	}
	if strings.ToLower(journalMode) != "wal" {
		_ = db.Close()
		return nil, fmt.Errorf("WAL mode was not enabled: %s", journalMode)
	}
	if err := initializeStore(db, seedCount); err != nil {
		_ = db.Close()
		return nil, err
	}
	statements := []string{
		`SELECT id, name, category, price_cents, stock, tags
FROM products WHERE id = ?`,
		`SELECT COUNT(*) FROM products
WHERE (? = '' OR category = ?)
  AND (? = '' OR instr(lower(name), lower(?)) > 0)`,
		`SELECT id, name, category, price_cents, stock, tags
FROM products
WHERE (? = '' OR category = ?)
  AND (? = '' OR instr(lower(name), lower(?)) > 0)
ORDER BY id LIMIT ? OFFSET ?`,
		`SELECT category, COUNT(*), COALESCE(SUM(stock), 0),
  COALESCE(SUM(stock * price_cents), 0)
FROM products GROUP BY category`,
		`SELECT type, SUM(count), SUM(value_total)
FROM event_totals GROUP BY type`,
		`INSERT INTO event_totals(user_id, type, count, value_total)
VALUES (?, ?, 1, ?)
ON CONFLICT(user_id, type) DO UPDATE SET
  count = count + 1,
  value_total = value_total + excluded.value_total`,
	}
	prepared := make([]*sql.Stmt, 0, len(statements))
	for _, statement := range statements {
		preparedStatement, err := db.Prepare(statement)
		if err != nil {
			for _, item := range prepared {
				_ = item.Close()
			}
			_ = db.Close()
			return nil, err
		}
		prepared = append(prepared, preparedStatement)
	}
	return &Store{
		db:            db,
		detail:        prepared[0],
		listCount:     prepared[1],
		listRows:      prepared[2],
		catalogReport: prepared[3],
		eventsReport:  prepared[4],
		eventUpsert:   prepared[5],
	}, nil
}

func initializeStore(db *sql.DB, seedCount int) error {
	var version string
	err := db.QueryRow("SELECT value FROM metadata WHERE key='schema_version'").Scan(&version)
	if err == nil {
		var storedSeed string
		if err := db.QueryRow("SELECT value FROM metadata WHERE key='seed_count'").Scan(&storedSeed); err != nil {
			return err
		}
		if version != schemaVersion || storedSeed != fmt.Sprint(seedCount) {
			return fmt.Errorf("database metadata does not match schema version %s and SEED_COUNT %d", schemaVersion, seedCount)
		}
		return nil
	}
	if !errors.Is(err, sql.ErrNoRows) && !strings.Contains(err.Error(), "no such table") {
		return err
	}
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	schema := `
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE products(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
  price_cents INTEGER NOT NULL, stock INTEGER NOT NULL, tags TEXT NOT NULL
);
CREATE INDEX products_category_id ON products(category, id);
CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE event_totals(
  user_id INTEGER NOT NULL REFERENCES users(id), type TEXT NOT NULL,
  count INTEGER NOT NULL CHECK(count >= 0), value_total INTEGER NOT NULL CHECK(value_total >= 0),
  PRIMARY KEY(user_id, type)
);`
	if _, err := tx.Exec(schema); err != nil {
		return err
	}
	products, err := tx.Prepare("INSERT INTO products(id,name,category,price_cents,stock,tags) VALUES(?,?,?,?,?,?)")
	if err != nil {
		return err
	}
	defer func() { _ = products.Close() }()
	users, err := tx.Prepare("INSERT INTO users(id,name) VALUES(?,?)")
	if err != nil {
		return err
	}
	defer func() { _ = users.Close() }()
	for id := 1; id <= seedCount; id++ {
		product := productFor(id)
		tags, _ := json.Marshal(product.Tags)
		if _, err := products.Exec(product.ID, product.Name, product.Category, product.PriceCents, product.Stock, string(tags)); err != nil {
			return err
		}
		if _, err := users.Exec(id, fmt.Sprintf("User %05d", id)); err != nil {
			return err
		}
	}
	if _, err := tx.Exec("INSERT INTO metadata(key,value) VALUES('schema_version',?),('seed_count',?)", schemaVersion, fmt.Sprint(seedCount)); err != nil {
		return err
	}
	return tx.Commit()
}

func (store *Store) close() error {
	for _, statement := range []*sql.Stmt{store.detail, store.listCount, store.listRows, store.catalogReport, store.eventsReport, store.eventUpsert} {
		_ = statement.Close()
	}
	return store.db.Close()
}

func (store *Store) sqliteVersion() (string, error) {
	var version string
	err := store.db.QueryRow("SELECT sqlite_version()").Scan(&version)
	return version, err
}

func (store *Store) pragma(name string) (string, error) {
	var value string
	err := store.db.QueryRow("PRAGMA " + name).Scan(&value)
	return value, err
}

func scanProduct(row interface{ Scan(...any) error }) (Product, error) {
	var product Product
	var tags string
	if err := row.Scan(&product.ID, &product.Name, &product.Category, &product.PriceCents, &product.Stock, &tags); err != nil {
		return Product{}, err
	}
	if err := json.Unmarshal([]byte(tags), &product.Tags); err != nil {
		return Product{}, err
	}
	return product, nil
}

func (store *Store) product(context context.Context, id int) (Product, bool, error) {
	product, err := scanProduct(store.detail.QueryRowContext(context, id))
	if errors.Is(err, sql.ErrNoRows) {
		return Product{}, false, nil
	}
	return product, err == nil, err
}
