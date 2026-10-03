package main

import (
	"fmt"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"

	"github.com/gofiber/fiber/v3"
)

func main() {
	runtime.GOMAXPROCS(1)
	framework := strings.ToLower(os.Getenv("FRAMEWORK"))
	if framework == "" {
		framework = "nethttp"
	}
	if framework != "nethttp" && framework != "chi" && framework != "fiber" {
		panic("FRAMEWORK must be nethttp, chi, or fiber")
	}
	seed := envInt("SEED_COUNT", 5000, 100, 100000)
	port := envInt("PORT", 8080, 1, 65535)
	path := os.Getenv("SQLITE_PATH")
	if path == "" {
		path = "/data/benchmark.sqlite"
	}
	s, err := openStore(path, int64(seed))
	if err != nil {
		panic(err)
	}
	defer s.close()
	fmt.Fprintf(os.Stderr, "experiment=%s runtime=go framework=%s gomaxprocs=%d cgo_enabled=%t\n", experiment, framework, runtime.GOMAXPROCS(0), cgoEnabled())
	address := fmt.Sprintf("0.0.0.0:%d", port)
	if framework == "fiber" {
		if err := fiberHandler(s).Listen(address, fiber.ListenConfig{DisableStartupMessage: true, EnablePrefork: false}); err != nil {
			panic(err)
		}
		return
	}
	handler := http.Handler(standardHandler(s, framework))
	if framework == "chi" {
		handler = chiHandler(s)
	}
	if err := http.ListenAndServe(address, handler); err != nil {
		panic(err)
	}
}
func envInt(name string, fallback, min, max int) int {
	text := os.Getenv(name)
	if text == "" {
		return fallback
	}
	value, err := strconv.Atoi(text)
	if err != nil || value < min || value > max {
		panic(fmt.Sprintf("%s must be an integer between %d and %d", name, min, max))
	}
	return value
}
