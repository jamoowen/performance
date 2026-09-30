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
	store, err := openStore(dbPath, seedCount, connections)
	if err != nil {
		panic(err)
	}
	defer func() { _ = store.close() }()
	version, err := store.sqliteVersion()
	if err != nil {
		panic(err)
	}
	journalMode, err := store.pragma("journal_mode")
	if err != nil {
		panic(err)
	}
	synchronous, err := store.pragma("synchronous")
	if err != nil {
		panic(err)
	}
	fmt.Fprintf(os.Stderr,
		"go_version=%s sqlite_version=%s seed_count=%d db_path=%s max_open_conns=%d journal_mode=%s synchronous=%s foreign_keys=on busy_timeout=5000 cache_size=-2000 wal_autocheckpoint=1000 temp_store=MEMORY\n",
		runtime.Version(), version, seedCount, dbPath, connections, journalMode, synchronous,
	)
	server := &http.Server{
		Addr:              fmt.Sprintf("0.0.0.0:%d", port),
		Handler:           newServer(store, seedCount),
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
