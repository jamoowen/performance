package main

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/gofiber/fiber/v3"
)

func writeHTTP(w http.ResponseWriter, r result, timing bool) {
	w.Header().Set("Content-Type", "application/json")
	if timing {
		w.Header().Set("Server-Timing", timingHeader(r))
	}
	w.WriteHeader(r.status)
	_, _ = w.Write(r.body)
}

func parseListQuery(raw string, seed int64) (int64, int64, error) {
	values, err := parseStrictQuery(raw)
	if err != nil {
		return 0, 0, err
	}
	offset, limit := int64(0), int64(20)
	if value, ok := values["offset"]; ok {
		offset, err = parseInt(value, "offset", 0, seed)
		if err != nil {
			return 0, 0, err
		}
	}
	if value, ok := values["limit"]; ok {
		limit, err = parseInt(value, "limit", 1, 100)
		if err != nil {
			return 0, 0, err
		}
	}
	return offset, limit, nil
}

func parseStrictQuery(raw string) (map[string]string, error) {
	values := map[string]string{}
	if raw == "" {
		return values, nil
	}
	for _, part := range strings.Split(raw, "&") {
		pair := strings.SplitN(part, "=", 2)
		if len(pair) != 2 || (pair[0] != "offset" && pair[0] != "limit") {
			return nil, errBadQuery
		}
		if _, exists := values[pair[0]]; exists {
			return nil, errBadQuery
		}
		values[pair[0]] = pair[1]
	}
	return values, nil
}

var errBadQuery = &inputError{"invalid query parameters"}

type inputError struct{ message string }

func (e *inputError) Error() string { return e.message }

func parseStock(contentType string, body io.Reader) (int64, result) {
	if !strings.EqualFold(strings.TrimSpace(strings.Split(contentType, ";")[0]), "application/json") {
		return 0, errResult(415, "content type must be application/json")
	}
	limited := io.LimitReader(body, maxBody+1)
	data, err := io.ReadAll(limited)
	if err != nil {
		return 0, errResult(400, "invalid JSON body")
	}
	if len(data) > maxBody {
		return 0, errResult(413, "body exceeds 65536 bytes")
	}
	var object map[string]json.RawMessage
	if json.Unmarshal(data, &object) != nil || len(object) != 1 || object["delta"] == nil {
		return 0, errResult(400, "body must be exactly {delta: integer}")
	}
	var delta int64
	decoder := json.NewDecoder(strings.NewReader(string(object["delta"])))
	decoder.UseNumber()
	var value any
	if decoder.Decode(&value) != nil {
		return 0, errResult(400, "delta must be an integer")
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return 0, errResult(400, "delta must be an integer")
	}
	number, ok := value.(json.Number)
	if !ok {
		return 0, errResult(400, "delta must be an integer")
	}
	parsed, err := number.Int64()
	if err != nil || parsed < -100 || parsed > 100 {
		return 0, errResult(400, "delta must be an integer between -100 and 100")
	}
	delta = parsed
	return delta, result{}
}

func standardHandler(s *store, framework string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeHTTP(w, result{200, jsonBytes(map[string]string{"status": "ok"}), 0, 0}, false)
	})
	mux.HandleFunc("GET /benchmark/info", func(w http.ResponseWriter, _ *http.Request) { writeHTTP(w, s.infoResult(framework), false) })
	mux.HandleFunc("GET /benchmark/integrity", func(w http.ResponseWriter, _ *http.Request) { writeHTTP(w, s.integrityResult(), false) })
	mux.HandleFunc("GET /products/{id}", func(w http.ResponseWriter, r *http.Request) {
		id, err := parseID(r.PathValue("id"))
		if err != nil {
			writeHTTP(w, errResult(400, err.Error()), false)
			return
		}
		out := s.detailResult(id)
		writeHTTP(w, out, out.status == 200)
	})
	mux.HandleFunc("GET /products", func(w http.ResponseWriter, r *http.Request) {
		offset, limit, err := parseListQuery(r.URL.RawQuery, s.seed)
		if err != nil {
			writeHTTP(w, errResult(400, err.Error()), false)
			return
		}
		out := s.listResult(offset, limit)
		writeHTTP(w, out, out.status == 200)
	})
	mux.HandleFunc("POST /products/{id}/stock", func(w http.ResponseWriter, r *http.Request) {
		id, err := parseID(r.PathValue("id"))
		if err != nil {
			writeHTTP(w, errResult(400, err.Error()), false)
			return
		}
		delta, bad := parseStock(r.Header.Get("Content-Type"), r.Body)
		if bad.status != 0 {
			writeHTTP(w, bad, false)
			return
		}
		out := s.updateResult(id, delta)
		writeHTTP(w, out, out.status == 200)
	})
	return mux
}

func chiHandler(s *store) http.Handler {
	r := chi.NewRouter()
	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeHTTP(w, result{200, jsonBytes(map[string]string{"status": "ok"}), 0, 0}, false)
	})
	r.Get("/benchmark/info", func(w http.ResponseWriter, _ *http.Request) { writeHTTP(w, s.infoResult("chi"), false) })
	r.Get("/benchmark/integrity", func(w http.ResponseWriter, _ *http.Request) { writeHTTP(w, s.integrityResult(), false) })
	r.Get("/products/{id}", func(w http.ResponseWriter, r *http.Request) {
		id, err := parseID(chi.URLParam(r, "id"))
		if err != nil {
			writeHTTP(w, errResult(400, err.Error()), false)
			return
		}
		out := s.detailResult(id)
		writeHTTP(w, out, out.status == 200)
	})
	r.Get("/products", func(w http.ResponseWriter, r *http.Request) {
		offset, limit, err := parseListQuery(r.URL.RawQuery, s.seed)
		if err != nil {
			writeHTTP(w, errResult(400, err.Error()), false)
			return
		}
		out := s.listResult(offset, limit)
		writeHTTP(w, out, out.status == 200)
	})
	r.Post("/products/{id}/stock", func(w http.ResponseWriter, r *http.Request) {
		id, err := parseID(chi.URLParam(r, "id"))
		if err != nil {
			writeHTTP(w, errResult(400, err.Error()), false)
			return
		}
		delta, bad := parseStock(r.Header.Get("Content-Type"), r.Body)
		if bad.status != 0 {
			writeHTTP(w, bad, false)
			return
		}
		out := s.updateResult(id, delta)
		writeHTTP(w, out, out.status == 200)
	})
	return r
}

func fiberHandler(s *store) *fiber.App {
	app := fiber.New()
	write := func(c fiber.Ctx, out result, timing bool) error {
		c.Set("Content-Type", "application/json")
		if timing {
			c.Set("Server-Timing", timingHeader(out))
		}
		return c.Status(out.status).Send(out.body)
	}
	app.Get("/healthz", func(c fiber.Ctx) error {
		return write(c, result{200, jsonBytes(map[string]string{"status": "ok"}), 0, 0}, false)
	})
	app.Get("/benchmark/info", func(c fiber.Ctx) error { return write(c, s.infoResult("fiber"), false) })
	app.Get("/benchmark/integrity", func(c fiber.Ctx) error { return write(c, s.integrityResult(), false) })
	app.Get("/products/:id", func(c fiber.Ctx) error {
		id, err := parseID(c.Params("id"))
		if err != nil {
			return write(c, errResult(400, err.Error()), false)
		}
		out := s.detailResult(id)
		return write(c, out, out.status == 200)
	})
	app.Get("/products", func(c fiber.Ctx) error {
		offset, limit, err := parseListQuery(c.Request().URI().QueryArgs().String(), s.seed)
		if err != nil {
			return write(c, errResult(400, err.Error()), false)
		}
		out := s.listResult(offset, limit)
		return write(c, out, out.status == 200)
	})
	app.Post("/products/:id/stock", func(c fiber.Ctx) error {
		id, err := parseID(c.Params("id"))
		if err != nil {
			return write(c, errResult(400, err.Error()), false)
		}
		delta, bad := parseStock(c.Get("Content-Type"), strings.NewReader(string(c.Body())))
		if bad.status != 0 {
			return write(c, bad, false)
		}
		out := s.updateResult(id, delta)
		return write(c, out, out.status == 200)
	})
	return app
}
