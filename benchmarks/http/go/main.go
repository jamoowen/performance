package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func main() {
	seedCount, err := positiveEnv("SEED_COUNT", 5000, 100000)
	if err != nil {
		panic(err)
	}
	port, err := positiveEnv("PORT", 8080, 65535)
	if err != nil {
		panic(err)
	}
	connections, err := positiveEnv("MAX_OPEN_CONNS", 1, 32)
	if err != nil {
		panic(err)
	}
	dbPath := os.Getenv("DB_PATH")
	if dbPath == "" {
		dbPath = filepath.Join("data", "benchmark.sqlite")
	}
	backendName := strings.ToLower(os.Getenv("BACKEND"))
	if backendName == "" {
		backendName = "sqlite"
	}
	var store backend
	switch backendName {
	case "sqlite":
		store, err = openStore(dbPath, seedCount, connections)
	case "memory":
		store = newMemoryStore(seedCount)
	default:
		panic("BACKEND must be sqlite or memory")
	}
	if err != nil {
		panic(err)
	}
	defer func() { _ = store.close() }()
	stopDiagnostics, err := startDiagnostics(store)
	if err != nil {
		panic(err)
	}
	defer func() { _ = stopDiagnostics() }()
	if sqlite, ok := store.(*Store); ok {
		version, err := sqlite.sqliteVersion()
		if err != nil {
			panic(err)
		}
		journalMode, err := sqlite.pragma("journal_mode")
		if err != nil {
			panic(err)
		}
		synchronous, err := sqlite.pragma("synchronous")
		if err != nil {
			panic(err)
		}
		fmt.Fprintf(os.Stderr, "go_version=%s backend=sqlite sqlite_version=%s seed_count=%d db_path=%s max_open_conns=%d journal_mode=%s synchronous=%s foreign_keys=on busy_timeout=5000 cache_size=-2000 wal_autocheckpoint=1000 temp_store=MEMORY\n", runtime.Version(), version, seedCount, dbPath, connections, journalMode, synchronous)
	} else {
		fmt.Fprintf(os.Stderr, "go_version=%s backend=memory seed_count=%d\n", runtime.Version(), seedCount)
	}
	router := strings.ToLower(os.Getenv("ROUTER"))
	if router == "" {
		router = "stdlib"
	}
	handler, err := newServerWithRouter(store, seedCount, router)
	if err != nil {
		panic(err)
	}
	server := &http.Server{
		Addr:              fmt.Sprintf("0.0.0.0:%d", port),
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	stop := make(chan os.Signal, 1)
	shutdownDone := make(chan struct{})
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-stop
		shutdownContext, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownContext); err != nil {
			_ = server.Close()
		}
		_ = stopDiagnostics()
		close(shutdownDone)
	}()
	err = server.ListenAndServe()
	if !errors.Is(err, http.ErrServerClosed) {
		panic(err)
	}
	<-shutdownDone
}

func positiveEnv(name string, fallback, maximum int) (int, error) {
	value := os.Getenv(name)
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 1 || parsed > maximum {
		return 0, fmt.Errorf("%s must be an integer between 1 and %d", name, maximum)
	}
	return parsed, nil
}
