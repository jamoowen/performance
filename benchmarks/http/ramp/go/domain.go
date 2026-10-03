package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

const (
	experiment = "sqlite-ramp-v2"
	maxBody    = 65536
)

var categories = []string{"books", "electronics", "home", "outdoors", "clothing"}

type product struct {
	ID         int64  `json:"id"`
	Name       string `json:"name"`
	Category   string `json:"category"`
	PriceCents int64  `json:"priceCents"`
	Stock      int64  `json:"stock"`
	Revision   int64  `json:"revision"`
}

type stockResult struct {
	ID       int64 `json:"id"`
	Stock    int64 `json:"stock"`
	Revision int64 `json:"revision"`
}

type result struct {
	status  int
	body    []byte
	service time.Duration
	db      time.Duration
}

type store struct {
	db        *sql.DB
	seed      int64
	mu        sync.Mutex
	detail    *sql.Stmt
	list      *sql.Stmt
	update    *sql.Stmt
	integrity *sql.Stmt
	pragmas   map[string]any
}

func openStore(path string, seed int64) (*store, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	s := &store{db: db, seed: seed}
	fail := func(err error) (*store, error) { s.close(); return nil, err }
	if err := s.initialize(); err != nil {
		return fail(err)
	}
	if s.detail, err = db.Prepare("SELECT id,name,category,price_cents,stock,revision FROM products WHERE id=?"); err != nil {
		return fail(err)
	}
	if s.list, err = db.Prepare("SELECT id,name,category,price_cents,stock,revision FROM products ORDER BY id LIMIT ? OFFSET ?"); err != nil {
		return fail(err)
	}
	if s.update, err = db.Prepare("UPDATE products SET stock=stock+?, revision=revision+1 WHERE id=? RETURNING id,stock,revision"); err != nil {
		return fail(err)
	}
	s.integrity, err = db.Prepare("SELECT COUNT(*), COALESCE(SUM(stock),0), COALESCE(SUM(revision),0) FROM products")
	if err != nil {
		return fail(err)
	}
	s.pragmas, err = s.readPragmas()
	if err != nil {
		return fail(err)
	}
	return s, nil
}

func (s *store) readPragmas() (map[string]any, error) {
	queries := map[string]string{"journal_mode": "journal_mode", "synchronous": "synchronous", "foreign_keys": "foreign_keys", "busy_timeout": "busy_timeout", "cache_size": "cache_size", "wal_autocheckpoint": "wal_autocheckpoint", "temp_store": "temp_store"}
	values := make(map[string]any, len(queries))
	for name, pragma := range queries {
		var value string
		if err := s.db.QueryRow("PRAGMA " + pragma).Scan(&value); err != nil {
			return nil, err
		}
		if name == "journal_mode" {
			values[name] = value
		} else {
			parsed, err := strconv.ParseInt(value, 10, 64)
			if err != nil {
				return nil, err
			}
			values[name] = parsed
		}
	}
	return values, nil
}

func (s *store) initialize() error {
	for _, pragma := range []string{"PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA foreign_keys=ON", "PRAGMA busy_timeout=5000", "PRAGMA cache_size=-2000", "PRAGMA wal_autocheckpoint=1000", "PRAGMA temp_store=MEMORY"} {
		if _, err := s.db.Exec(pragma); err != nil {
			return err
		}
	}
	if _, err := s.db.Exec(`CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price_cents INTEGER NOT NULL, stock INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0)`); err != nil {
		return err
	}
	var count int64
	if err := s.db.QueryRow("SELECT COUNT(*) FROM products").Scan(&count); err != nil {
		return err
	}
	if count != 0 && count != s.seed {
		return fmt.Errorf("existing products row count %d does not match SEED_COUNT %d", count, s.seed)
	}
	if count != 0 {
		return nil
	}
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	stmt, err := tx.Prepare("INSERT INTO products(id,name,category,price_cents,stock,revision) VALUES(?,?,?,?,?,0)")
	if err != nil {
		_ = tx.Rollback()
		return err
	}
	defer func() { _ = stmt.Close() }()
	for id := int64(1); id <= s.seed; id++ {
		if _, err := stmt.Exec(id, fmt.Sprintf("Product%05d", id), categories[(id-1)%int64(len(categories))], 500+(id*7919)%50000, (id*37)%201); err != nil {
			_ = tx.Rollback()
			return err
		}
	}
	return tx.Commit()
}

func (s *store) close() {
	if s.detail != nil {
		_ = s.detail.Close()
	}
	if s.list != nil {
		_ = s.list.Close()
	}
	if s.update != nil {
		_ = s.update.Close()
	}
	if s.integrity != nil {
		_ = s.integrity.Close()
	}
	if s.db != nil {
		_ = s.db.Close()
	}
}

func parseID(text string) (int64, error) {
	if text == "" || strings.HasPrefix(text, "+") || strings.HasPrefix(text, "-") {
		return 0, fmt.Errorf("id must be a positive integer")
	}
	id, err := strconv.ParseInt(text, 10, 64)
	if err != nil || id < 1 || id > 9007199254740991 {
		return 0, fmt.Errorf("id must be a positive integer")
	}
	return id, nil
}

func parseInt(text, name string, min, max int64) (int64, error) {
	v, err := strconv.ParseInt(text, 10, 64)
	if err != nil || v < min || v > max {
		return 0, fmt.Errorf("%s is out of range", name)
	}
	return v, nil
}

func jsonBytes(value any) []byte { data, _ := json.Marshal(value); return data }
func errResult(status int, message string) result {
	return result{status: status, body: jsonBytes(map[string]string{"error": message})}
}

func (s *store) detailResult(id int64) result {
	started := time.Now()
	dbStarted := time.Now()
	s.mu.Lock()
	var p product
	err := s.detail.QueryRow(id).Scan(&p.ID, &p.Name, &p.Category, &p.PriceCents, &p.Stock, &p.Revision)
	s.mu.Unlock()
	dbElapsed := time.Since(dbStarted)
	if err == sql.ErrNoRows {
		return errResult(404, "product not found")
	}
	if err != nil {
		return errResult(500, "database error")
	}
	return result{status: 200, body: jsonBytes(p), service: time.Since(started), db: dbElapsed}
}

func (s *store) listResult(offset, limit int64) result {
	started := time.Now()
	dbStarted := time.Now()
	s.mu.Lock()
	rows, err := s.list.Query(limit, offset)
	if err != nil {
		s.mu.Unlock()
		return errResult(500, "database error")
	}
	products := make([]product, 0, limit)
	for rows.Next() {
		var p product
		if err = rows.Scan(&p.ID, &p.Name, &p.Category, &p.PriceCents, &p.Stock, &p.Revision); err != nil {
			break
		}
		products = append(products, p)
	}
	closeErr := rows.Close()
	s.mu.Unlock()
	dbElapsed := time.Since(dbStarted)
	if err != nil || rows.Err() != nil || closeErr != nil {
		return errResult(500, "database error")
	}
	return result{status: 200, body: jsonBytes(map[string]any{"products": products, "total": s.seed, "offset": offset, "limit": limit}), service: time.Since(started), db: dbElapsed}
}

func (s *store) updateResult(id, delta int64) result {
	started := time.Now()
	dbStarted := time.Now()
	s.mu.Lock()
	var updated stockResult
	err := s.update.QueryRow(delta, id).Scan(&updated.ID, &updated.Stock, &updated.Revision)
	s.mu.Unlock()
	dbElapsed := time.Since(dbStarted)
	if err == sql.ErrNoRows {
		return errResult(404, "product not found")
	}
	if err != nil {
		return errResult(500, "database error")
	}
	return result{status: 200, body: jsonBytes(updated), service: time.Since(started), db: dbElapsed}
}

func (s *store) integrityResult() result {
	var rows, stock, revisions int64
	s.mu.Lock()
	err := s.integrity.QueryRow().Scan(&rows, &stock, &revisions)
	s.mu.Unlock()
	if err != nil {
		return errResult(500, "database error")
	}
	return result{status: 200, body: jsonBytes(map[string]int64{"rows": rows, "totalStock": stock, "totalRevisions": revisions})}
}

func (s *store) infoResult(framework string) result {
	var version string
	_ = s.db.QueryRow("SELECT sqlite_version()").Scan(&version)
	options := []string{}
	rows, err := s.db.Query("PRAGMA compile_options")
	if err == nil {
		for rows.Next() {
			var option string
			if rows.Scan(&option) == nil {
				options = append(options, option)
			}
		}
		_ = rows.Close()
	}
	return result{status: 200, body: jsonBytes(map[string]any{"experiment": experiment, "runtime": "go", "framework": framework, "runtimeVersion": runtimeVersion(), "frameworkVersion": frameworkVersion(framework), "driver": "modernc.org/sqlite", "driverVersion": "1.60.1", "sqliteVersion": version, "seedCount": s.seed, "workers": 1, "pragmas": s.pragmas, "cgoEnabled": cgoEnabled(), "gomaxprocs": runtime.GOMAXPROCS(0), "compileOptions": options})}
}
func cgoEnabled() bool {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return false
	}
	for _, setting := range info.Settings {
		if setting.Key == "CGO_ENABLED" {
			return setting.Value == "1"
		}
	}
	return false
}

func runtimeVersion() string { return strings.TrimPrefix(strings.TrimSpace(runtimeVersionRaw()), "go") }
func frameworkVersion(framework string) string {
	switch framework {
	case "chi":
		return "5.3.2"
	case "fiber":
		return "3.5.0"
	default:
		return "stdlib"
	}
}
func timingHeader(r result) string {
	return fmt.Sprintf("service;dur=%.3f, db;dur=%.3f", float64(r.service.Microseconds())/1000, float64(r.db.Microseconds())/1000)
}
