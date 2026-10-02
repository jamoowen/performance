package main

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

func TestDiagnosticsDisabledDoesNotListen(t *testing.T) {
	t.Setenv("DIAGNOSTICS", "0")
	listener, err := net.Listen("tcp", diagnosticsAddress)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = listener.Close() }()
	cleanup, err := startDiagnostics(nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := cleanup(); err != nil {
		t.Fatal(err)
	}
}

func TestDiagnosticsRejectsInvalidMode(t *testing.T) {
	t.Setenv("DIAGNOSTICS", "yes")
	if _, err := startDiagnostics(nil); err == nil {
		t.Fatal("expected invalid diagnostics mode error")
	}
}

func TestDiagnosticsHandlerRestrictsMethodsAndCPUSeconds(t *testing.T) {
	store := testDiagnosticsStore(t, 2)
	handler := newDiagnosticsHandler(store)
	for _, target := range []string{"/cpu?seconds=0", "/cpu?seconds=121", "/cpu?seconds=no"} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, target, nil))
		if response.Code != http.StatusBadRequest {
			t.Fatalf("%s status=%d", target, response.Code)
		}
	}
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/heap", nil))
	if response.Code != http.StatusMethodNotAllowed {
		t.Fatalf("method status=%d", response.Code)
	}
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/unknown", nil))
	if response.Code != http.StatusNotFound {
		t.Fatalf("unknown status=%d", response.Code)
	}
}

func TestDiagnosticsRuntimeReportsStoreAndWaits(t *testing.T) {
	store := testDiagnosticsStore(t, 1)
	connection, err := store.db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	acquired := make(chan error, 1)
	go func() {
		waiting, err := store.db.Conn(context.Background())
		if err == nil {
			_ = waiting.Close()
		}
		acquired <- err
	}()
	deadline := time.Now().Add(time.Second)
	for store.db.Stats().WaitCount == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if store.db.Stats().WaitCount == 0 {
		t.Fatal("database connection did not wait")
	}
	time.Sleep(10 * time.Millisecond)
	if err := connection.Close(); err != nil {
		t.Fatal(err)
	}
	if err := <-acquired; err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	newDiagnosticsHandler(store).ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/runtime", nil))
	if response.Code != http.StatusOK {
		t.Fatalf("runtime status=%d", response.Code)
	}
	var report runtimeDiagnostics
	if err := json.Unmarshal(response.Body.Bytes(), &report); err != nil {
		t.Fatal(err)
	}
	if report.SchemaVersion != 1 || report.Runtime != "go" || report.ProcessID != os.Getpid() || report.Database.MaxOpenConnections != 1 {
		t.Fatalf("unexpected runtime report: %+v", report)
	}
	if report.Database.WaitCount < 1 || report.Database.WaitDurationNS < 1 {
		t.Fatalf("wait metrics were not recorded: %+v", report.Database)
	}
	if report.Go.GOMAXPROCS != runtimeGOMAXPROCS() {
		t.Fatalf("gomaxprocs=%d", report.Go.GOMAXPROCS)
	}
}

func TestDiagnosticsCPUProfileAndShutdown(t *testing.T) {
	store := testDiagnosticsStore(t, 1)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	cleanup, _ := startDiagnosticsListenerWithCPU(store, listener)
	t.Cleanup(func() { _ = cleanup() })
	busyDone := make(chan struct{})
	go func() {
		defer close(busyDone)
		until := time.Now().Add(1200 * time.Millisecond)
		for time.Now().Before(until) {
		}
	}()
	response, err := http.Get("http://" + listener.Addr().String() + "/cpu?seconds=1")
	if err != nil {
		t.Fatal(err)
	}
	profile, err := io.ReadAll(response.Body)
	_ = response.Body.Close()
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || len(profile) == 0 {
		t.Fatalf("cpu profile status=%d bytes=%d", response.StatusCode, len(profile))
	}
	if profile[0] != 0x1f || profile[1] != 0x8b {
		t.Fatal("cpu profile is not gzip data")
	}
	assertPprofData(t, "cpu", profile)
	<-busyDone
	if err := cleanup(); err != nil {
		t.Fatal(err)
	}
	if _, err := net.DialTimeout("tcp", listener.Addr().String(), 100*time.Millisecond); err == nil {
		t.Fatal("diagnostics listener remained open")
	}
}

func TestDiagnosticsSnapshotProfilesAreValid(t *testing.T) {
	store := testDiagnosticsStore(t, 1)
	handler := newDiagnosticsHandler(store)
	for _, name := range []string{"heap", "allocs", "goroutine", "block", "mutex"} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/"+name, nil))
		if response.Code != http.StatusOK {
			t.Fatalf("%s status=%d", name, response.Code)
		}
		assertPprofData(t, name, response.Body.Bytes())
	}
}

func TestDiagnosticsCleanupCancelsActiveCapture(t *testing.T) {
	store := testDiagnosticsStore(t, 1)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	cleanup, cpu := startDiagnosticsListenerWithCPU(store, listener)
	t.Cleanup(func() { _ = cleanup() })
	responseDone := make(chan struct{})
	go func() {
		response, err := http.Get("http://" + listener.Addr().String() + "/cpu?seconds=120")
		if err == nil {
			_ = response.Body.Close()
		}
		close(responseDone)
	}()
	deadline := time.Now().Add(time.Second)
	for !cpu.active.Load() && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if !cpu.active.Load() {
		t.Fatal("cpu profiling did not start")
	}
	if err := cleanup(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-responseDone:
	case <-time.After(time.Second):
		t.Fatal("active cpu capture did not stop")
	}
	handler, cpu := newDiagnosticsHandlerWithCPU(store)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/cpu?seconds=1", nil))
	if response.Code != http.StatusOK || cpu.active.Load() {
		t.Fatalf("new cpu capture status=%d active=%t", response.Code, cpu.active.Load())
	}
}

func TestDiagnosticsCPUProfileRejectsConcurrentCapture(t *testing.T) {
	store := testDiagnosticsStore(t, 1)
	handler, cpu := newDiagnosticsHandlerWithCPU(store)
	request := httptest.NewRequest(http.MethodGet, "/cpu?seconds=1", nil)
	first := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		handler.ServeHTTP(first, request)
		close(done)
	}()
	deadline := time.Now().Add(time.Second)
	for !cpu.active.Load() && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if !cpu.active.Load() {
		t.Fatal("cpu profiling did not start")
	}
	second := httptest.NewRecorder()
	handler.ServeHTTP(second, httptest.NewRequest(http.MethodGet, "/cpu?seconds=1", nil))
	if second.Code != http.StatusConflict {
		t.Fatalf("concurrent status=%d", second.Code)
	}
	<-done
}

func assertPprofData(t *testing.T, name string, data []byte) {
	t.Helper()
	if len(data) < 2 || data[0] != 0x1f || data[1] != 0x8b {
		t.Fatalf("%s profile is not gzip data", name)
	}
	profilePath := filepath.Join(t.TempDir(), name+".pprof")
	if err := os.WriteFile(profilePath, data, 0600); err != nil {
		t.Fatal(err)
	}
	if output, err := exec.Command("go", "tool", "pprof", "-raw", profilePath).CombinedOutput(); err != nil {
		t.Fatalf("go tool pprof %s: %v: %s", name, err, output)
	}
}

func testDiagnosticsStore(t *testing.T, connections int) *Store {
	t.Helper()
	store, err := openStore(filepath.Join(t.TempDir(), "benchmark.sqlite"), 20, connections)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.close() })
	return store
}

func runtimeGOMAXPROCS() int {
	return runtime.GOMAXPROCS(0)
}

func TestDiagnosticsDefaultModeIsDisabled(t *testing.T) {
	t.Setenv("DIAGNOSTICS", "")
	cleanup, err := startDiagnostics(nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := cleanup(); err != nil {
		t.Fatal(err)
	}
}
