package main

import (
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"runtime"
	runtimepprof "runtime/pprof"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

const diagnosticsAddress = "127.0.0.1:6060"

type diagnosticsServer struct {
	listener net.Listener
	server   *http.Server
	cpu      *cpuProfiler
	cleanup  sync.Once
}

type cpuProfiler struct {
	guard    sync.Mutex
	state    sync.Mutex
	stop     chan struct{}
	done     chan struct{}
	shutting bool
	active   atomic.Bool
}

type processCPU struct {
	UserUS   int64 `json:"user_us"`
	SystemUS int64 `json:"system_us"`
}

type runtimeDiagnostics struct {
	SchemaVersion int         `json:"schema_version"`
	Runtime       string      `json:"runtime"`
	TimeUnix      float64     `json:"time_unix"`
	Capabilities  []string    `json:"capabilities"`
	ProcessID     int         `json:"process_id"`
	ProcessCPU    *processCPU `json:"process_cpu"`
	Go            goRuntime   `json:"go"`
	Database      database    `json:"database"`
}

type goRuntime struct {
	Version         string `json:"version"`
	GOMAXPROCS      int    `json:"gomaxprocs"`
	LogicalCPUs     int    `json:"logical_cpus"`
	Goroutines      int    `json:"goroutines"`
	CGOCalls        int64  `json:"cgo_calls"`
	HeapAllocBytes  uint64 `json:"heap_alloc_bytes"`
	HeapSysBytes    uint64 `json:"heap_sys_bytes"`
	TotalAllocBytes uint64 `json:"total_alloc_bytes"`
	HeapObjects     uint64 `json:"heap_objects"`
	GCCycles        uint32 `json:"gc_cycles"`
	GCPauseTotalNS  uint64 `json:"gc_pause_total_ns"`
}

type database struct {
	MaxOpenConnections int   `json:"max_open_connections"`
	OpenConnections    int   `json:"open_connections"`
	InUse              int   `json:"in_use"`
	Idle               int   `json:"idle"`
	WaitCount          int64 `json:"wait_count"`
	WaitDurationNS     int64 `json:"wait_duration_ns"`
}

func startDiagnostics(store *Store) (func() error, error) {
	mode := os.Getenv("DIAGNOSTICS")
	if mode == "" || mode == "0" {
		return func() error { return nil }, nil
	}
	if mode != "1" {
		return nil, fmt.Errorf("DIAGNOSTICS must be 0 or 1")
	}
	listener, err := net.Listen("tcp", diagnosticsAddress)
	if err != nil {
		return nil, fmt.Errorf("listen diagnostics: %w", err)
	}
	cleanup, _ := startDiagnosticsListenerWithCPU(store, listener)
	return cleanup, nil
}

func startDiagnosticsListenerWithCPU(store *Store, listener net.Listener) (func() error, *cpuProfiler) {
	runtime.SetBlockProfileRate(1_000_000)
	runtime.SetMutexProfileFraction(10)
	handler, cpu := newDiagnosticsHandlerWithCPU(store)
	server := &http.Server{Handler: handler}
	diagnostics := diagnosticsServer{listener: listener, server: server, cpu: cpu}
	go func() {
		err := server.Serve(listener)
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			fmt.Fprintf(os.Stderr, "diagnostics server error: %v\n", err)
		}
	}()
	return func() error {
		var err error
		diagnostics.cleanup.Do(func() {
			runtime.SetBlockProfileRate(0)
			runtime.SetMutexProfileFraction(0)
			diagnostics.cpu.stopCapture()
			err = diagnostics.server.Close()
		})
		return err
	}, cpu
}

func newDiagnosticsHandler(store *Store) http.Handler {
	handler, _ := newDiagnosticsHandlerWithCPU(store)
	return handler
}

func newDiagnosticsHandlerWithCPU(store *Store) (http.Handler, *cpuProfiler) {
	cpu := &cpuProfiler{}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /runtime", func(writer http.ResponseWriter, request *http.Request) {
		writeDiagnosticsRuntime(writer, store)
	})
	mux.HandleFunc("GET /cpu", cpu.profile)
	for _, profile := range []string{"heap", "allocs", "goroutine", "block", "mutex"} {
		mux.Handle("GET /"+profile, snapshotProfile(profile))
	}
	mux.HandleFunc("/", func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet {
			methodNotAllowed(writer, http.MethodGet)
			return
		}
		writeError(writer, http.StatusNotFound, "not found")
	})
	return mux, cpu
}

func (cpu *cpuProfiler) profile(writer http.ResponseWriter, request *http.Request) {
	seconds, ok := diagnosticsSeconds(request.URL.Query().Get("seconds"))
	if !ok {
		writeError(writer, http.StatusBadRequest, "seconds must be an integer between 1 and 120")
		return
	}
	if !cpu.guard.TryLock() {
		writeError(writer, http.StatusConflict, "cpu profile already running")
		return
	}
	defer cpu.guard.Unlock()
	stop := make(chan struct{})
	done := make(chan struct{})
	cpu.state.Lock()
	if cpu.shutting {
		cpu.state.Unlock()
		writeError(writer, http.StatusServiceUnavailable, "diagnostics are shutting down")
		return
	}
	cpu.stop = stop
	cpu.done = done
	cpu.state.Unlock()
	cpu.active.Store(true)
	defer func() {
		cpu.active.Store(false)
		cpu.state.Lock()
		cpu.stop = nil
		cpu.done = nil
		close(done)
		cpu.state.Unlock()
	}()
	writer.Header().Set("Content-Type", "application/octet-stream")
	writer.Header().Set("Content-Disposition", "attachment; filename=profile")
	if err := runtimepprof.StartCPUProfile(writer); err != nil {
		writeError(writer, http.StatusInternalServerError, "start cpu profile")
		return
	}
	defer func() {
		runtimepprof.StopCPUProfile()
	}()
	select {
	case <-time.After(time.Duration(seconds) * time.Second):
	case <-request.Context().Done():
	case <-stop:
	}
}

func (cpu *cpuProfiler) stopCapture() {
	cpu.state.Lock()
	cpu.shutting = true
	stop, done := cpu.stop, cpu.done
	if stop != nil {
		close(stop)
	}
	cpu.state.Unlock()
	if done != nil {
		<-done
	}
}

func snapshotProfile(name string) http.Handler {
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		profile := runtimepprof.Lookup(name)
		if profile == nil {
			writeError(writer, http.StatusNotFound, "profile not found")
			return
		}
		writer.Header().Set("Content-Type", "application/octet-stream")
		if err := profile.WriteTo(writer, 0); err != nil {
			writeError(writer, http.StatusInternalServerError, "write profile")
		}
	})
}

func diagnosticsSeconds(value string) (int, bool) {
	if value == "" {
		return 30, true
	}
	seconds, err := strconv.Atoi(value)
	return seconds, err == nil && seconds >= 1 && seconds <= 120
}

func writeDiagnosticsRuntime(writer http.ResponseWriter, store *Store) {
	var memory runtime.MemStats
	runtime.ReadMemStats(&memory)
	dbStats := store.db.Stats()
	writeJSON(writer, http.StatusOK, runtimeDiagnostics{
		SchemaVersion: 1,
		Runtime:       "go",
		TimeUnix:      float64(time.Now().UnixNano()) / float64(time.Second),
		Capabilities:  []string{"cpu", "heap", "allocs", "goroutine", "block", "mutex"},
		ProcessID:     os.Getpid(),
		ProcessCPU:    currentProcessCPU(),
		Go: goRuntime{
			Version:         runtime.Version(),
			GOMAXPROCS:      runtime.GOMAXPROCS(0),
			LogicalCPUs:     runtime.NumCPU(),
			Goroutines:      runtime.NumGoroutine(),
			CGOCalls:        runtime.NumCgoCall(),
			HeapAllocBytes:  memory.HeapAlloc,
			HeapSysBytes:    memory.HeapSys,
			TotalAllocBytes: memory.TotalAlloc,
			HeapObjects:     memory.HeapObjects,
			GCCycles:        memory.NumGC,
			GCPauseTotalNS:  memory.PauseTotalNs,
		},
		Database: database{
			MaxOpenConnections: dbStats.MaxOpenConnections,
			OpenConnections:    dbStats.OpenConnections,
			InUse:              dbStats.InUse,
			Idle:               dbStats.Idle,
			WaitCount:          dbStats.WaitCount,
			WaitDurationNS:     dbStats.WaitDuration.Nanoseconds(),
		},
	})
}
