package main

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/go-chi/chi/v5"
)

const maxBodyBytes = 1 << 20

type apiServer struct {
	store     backend
	seedCount int
}

func newServer(store backend, seedCount int) http.Handler {
	server := apiServer{store: store, seedCount: seedCount}
	mux := http.NewServeMux()
	registerGetRoute(mux, "/healthz", func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(writer, http.StatusOK, map[string]string{"status": "ok"})
	})
	registerGetRoute(mux, "/products", server.listProducts)
	registerGetRoute(mux, "/products/{id}", server.productDetail)
	registerGetRoute(mux, "/products/{$}", func(writer http.ResponseWriter, request *http.Request) {
		writeError(writer, http.StatusBadRequest, "id must be a positive integer")
	})
	registerGetRoute(mux, "/reports/catalog", server.catalogReport)
	registerGetRoute(mux, "/reports/events", server.eventsReport)
	registerPostRoute(mux, "/cart/quote", server.quoteCart)
	registerPostRoute(mux, "/events/batch", server.batchEvents)
	mux.HandleFunc("/", func(writer http.ResponseWriter, request *http.Request) {
		writeError(writer, http.StatusNotFound, "not found")
	})
	return mux
}

func newServerWithRouter(store backend, seedCount int, router string) (http.Handler, error) {
	if router == "stdlib" {
		return newServer(store, seedCount), nil
	}
	if router != "chi" {
		return nil, fmt.Errorf("ROUTER must be stdlib or chi")
	}
	server := apiServer{store: store, seedCount: seedCount}
	routes := chi.NewRouter()
	productHandler := func(writer http.ResponseWriter, request *http.Request) {
		id, err := url.PathUnescape(chi.URLParam(request, "id"))
		if err != nil {
			writeError(writer, http.StatusBadRequest, "id must be a positive integer")
			return
		}
		request.SetPathValue("id", id)
		server.productDetail(writer, request)
	}
	routes.Get("/healthz", func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(writer, http.StatusOK, map[string]string{"status": "ok"})
	})
	routes.Head("/healthz", func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(writer, http.StatusOK, map[string]string{"status": "ok"})
	})
	routes.Get("/products", server.listProducts)
	routes.Head("/products", server.listProducts)
	routes.Get("/products/{id}", productHandler)
	routes.Head("/products/{id}", productHandler)
	routes.Get("/products/", func(writer http.ResponseWriter, request *http.Request) {
		writeError(writer, http.StatusBadRequest, "id must be a positive integer")
	})
	routes.Head("/products/", func(writer http.ResponseWriter, request *http.Request) {
		writeError(writer, http.StatusBadRequest, "id must be a positive integer")
	})
	routes.Get("/reports/catalog", server.catalogReport)
	routes.Head("/reports/catalog", server.catalogReport)
	routes.Get("/reports/events", server.eventsReport)
	routes.Head("/reports/events", server.eventsReport)
	routes.Post("/cart/quote", server.quoteCart)
	routes.Post("/events/batch", server.batchEvents)
	routes.NotFound(func(writer http.ResponseWriter, request *http.Request) {
		writeError(writer, http.StatusNotFound, "not found")
	})
	routes.MethodNotAllowed(func(writer http.ResponseWriter, request *http.Request) {
		path := request.URL.Path
		if path == "/healthz" || path == "/products" || path == "/products/" || path == "/reports/catalog" || path == "/reports/events" || strings.HasPrefix(path, "/products/") && !strings.Contains(strings.TrimPrefix(path, "/products/"), "/") {
			methodNotAllowed(writer, "GET, HEAD")
			return
		}
		if path == "/cart/quote" || path == "/events/batch" {
			methodNotAllowed(writer, http.MethodPost)
			return
		}
		writeError(writer, http.StatusNotFound, "not found")
	})
	return routes, nil
}

func registerGetRoute(mux *http.ServeMux, pattern string, handler http.HandlerFunc) {
	mux.HandleFunc("GET "+pattern, handler)
	mux.HandleFunc(pattern, func(writer http.ResponseWriter, request *http.Request) { methodNotAllowed(writer, "GET, HEAD") })
}

func registerPostRoute(mux *http.ServeMux, pattern string, handler http.HandlerFunc) {
	mux.HandleFunc("POST "+pattern, handler)
	mux.HandleFunc(pattern, func(writer http.ResponseWriter, request *http.Request) { methodNotAllowed(writer, http.MethodPost) })
}

func (server apiServer) productDetail(writer http.ResponseWriter, request *http.Request) {
	rawID := request.PathValue("id")
	id, ok := decimalParameter(rawID, 1, 1000000000)
	if !ok {
		writeError(writer, http.StatusBadRequest, "id must be a positive integer")
		return
	}
	product, found, err := server.store.product(request.Context(), id)
	if err != nil {
		databaseError(writer, err)
		return
	}
	if !found {
		writeError(writer, http.StatusNotFound, "product not found")
		return
	}
	writeJSON(writer, http.StatusOK, product)
}

func (server apiServer) listProducts(writer http.ResponseWriter, request *http.Request) {
	query := request.URL.Query()
	category := query.Get("category")
	search := strings.ToLower(query.Get("q"))
	if category != "" && !validCategory(category) {
		writeError(writer, http.StatusBadRequest, "category is invalid")
		return
	}
	offset, ok := paginationValue(query.Get("offset"), 0, 1000000000)
	if !ok {
		writeError(writer, http.StatusBadRequest, "offset is invalid")
		return
	}
	limit, ok := paginationValue(query.Get("limit"), 20, 100)
	if !ok || limit == 0 {
		writeError(writer, http.StatusBadRequest, "limit is invalid")
		return
	}
	result, err := server.store.list(request.Context(), category, search, offset, limit)
	if err != nil {
		databaseError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, struct {
		Products []Product `json:"products"`
		Total    int       `json:"total"`
		Offset   int       `json:"offset"`
		Limit    int       `json:"limit"`
	}{result.Products, result.Total, result.Offset, result.Limit})
}

func (server apiServer) catalogReport(writer http.ResponseWriter, request *http.Request) {
	result, err := server.store.catalog(request.Context())
	if err != nil {
		databaseError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, result)
}

type quoteLine struct {
	ProductID *int `json:"productId"`
	Quantity  *int `json:"quantity"`
}
type quoteRequest struct {
	Items  []quoteLine `json:"items"`
	Coupon *string     `json:"coupon"`
}

func (server apiServer) quoteCart(writer http.ResponseWriter, request *http.Request) {
	if !jsonContentType(request) {
		writeError(writer, http.StatusUnsupportedMediaType, "content-type must be application/json")
		return
	}
	var input quoteRequest
	if status := decodeJSON(writer, request, &input); status != 0 || len(input.Items) < 1 || len(input.Items) > 100 {
		if status == http.StatusRequestEntityTooLarge {
			writeError(writer, status, "request body too large")
			return
		}
		writeError(writer, http.StatusBadRequest, "invalid quote request")
		return
	}
	if input.Coupon != nil && *input.Coupon != "SAVE10" {
		writeError(writer, http.StatusBadRequest, "coupon is invalid")
		return
	}
	type resultLine struct {
		ProductID      int `json:"productId"`
		Quantity       int `json:"quantity"`
		UnitPriceCents int `json:"unitPriceCents"`
		LineTotalCents int `json:"lineTotalCents"`
	}
	lines := make([]resultLine, 0, len(input.Items))
	subtotal := 0
	requested := map[int]int{}
	for _, line := range input.Items {
		if line.ProductID == nil || line.Quantity == nil || *line.ProductID < 1 || *line.ProductID > 1000000000 || *line.Quantity < 1 || *line.Quantity > 100 {
			writeError(writer, http.StatusBadRequest, "quantity is invalid or unavailable")
			return
		}
		product, found, err := server.store.product(request.Context(), *line.ProductID)
		if err != nil {
			databaseError(writer, err)
			return
		}
		if !found {
			writeError(writer, http.StatusNotFound, "product not found")
			return
		}
		requested[*line.ProductID] += *line.Quantity
		if requested[*line.ProductID] > product.Stock {
			writeError(writer, http.StatusBadRequest, "quantity is invalid or unavailable")
			return
		}
		total := product.PriceCents * *line.Quantity
		subtotal += total
		lines = append(lines, resultLine{*line.ProductID, *line.Quantity, product.PriceCents, total})
	}
	discount := 0
	if input.Coupon != nil {
		discount = subtotal * 10 / 100
	}
	tax := (subtotal - discount) * 20 / 100
	writeJSON(writer, http.StatusOK, struct {
		Items         []resultLine `json:"items"`
		SubtotalCents int          `json:"subtotalCents"`
		DiscountCents int          `json:"discountCents"`
		TaxCents      int          `json:"taxCents"`
		TotalCents    int          `json:"totalCents"`
	}{lines, subtotal, discount, tax, subtotal - discount + tax})
}

type event struct {
	UserID *int64 `json:"userId"`
	Type   string `json:"type"`
	Value  *int64 `json:"value"`
}
type eventRequest struct {
	Events []event `json:"events"`
}

func (server apiServer) batchEvents(writer http.ResponseWriter, request *http.Request) {
	if !jsonContentType(request) {
		writeError(writer, http.StatusUnsupportedMediaType, "content-type must be application/json")
		return
	}
	var input eventRequest
	if status := decodeJSON(writer, request, &input); status != 0 || len(input.Events) < 1 || len(input.Events) > 100 {
		if status == http.StatusRequestEntityTooLarge {
			writeError(writer, status, "request body too large")
			return
		}
		writeError(writer, http.StatusBadRequest, "invalid events request")
		return
	}
	counts := map[string]int64{"view": 0, "click": 0, "purchase": 0}
	values := map[string]int64{"view": 0, "click": 0, "purchase": 0}
	canonical := strings.Builder{}
	for _, item := range input.Events {
		if item.UserID == nil || item.Value == nil || *item.UserID < 1 || *item.UserID > int64(server.seedCount) || *item.Value < 0 || *item.Value > 1000000 || !validEventType(item.Type) {
			writeError(writer, http.StatusBadRequest, "event is invalid")
			return
		}
		counts[item.Type]++
		values[item.Type] += *item.Value
		fmt.Fprintf(&canonical, "%d:%s:%d\n", *item.UserID, item.Type, *item.Value)
	}
	if err := server.store.recordEvents(request.Context(), input.Events); err != nil {
		databaseError(writer, err)
		return
	}
	digest := sha256.Sum256([]byte(canonical.String()))
	writeJSON(writer, http.StatusOK, struct {
		Counts map[string]int64 `json:"counts"`
		Values map[string]int64 `json:"values"`
		SHA256 string           `json:"sha256"`
	}{counts, values, hex.EncodeToString(digest[:])})
}

func (server apiServer) eventsReport(writer http.ResponseWriter, request *http.Request) {
	result, err := server.store.events(request.Context())
	if err != nil {
		databaseError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, result)
}

func decodeJSON(writer http.ResponseWriter, request *http.Request, target any) int {
	request.Body = http.MaxBytesReader(writer, request.Body, maxBodyBytes)
	decoder := json.NewDecoder(request.Body)
	if err := decoder.Decode(target); err != nil {
		var bodyTooLarge *http.MaxBytesError
		if errors.As(err, &bodyTooLarge) {
			return http.StatusRequestEntityTooLarge
		}
		return http.StatusBadRequest
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		var bodyTooLarge *http.MaxBytesError
		if errors.As(err, &bodyTooLarge) {
			return http.StatusRequestEntityTooLarge
		}
		return http.StatusBadRequest
	}
	return 0
}
func jsonContentType(request *http.Request) bool {
	return strings.EqualFold(strings.TrimSpace(strings.Split(request.Header.Get("Content-Type"), ";")[0]), "application/json")
}
func validCategory(value string) bool {
	for _, category := range categories {
		if value == category {
			return true
		}
	}
	return false
}
func validEventType(value string) bool {
	return value == "view" || value == "click" || value == "purchase"
}
func paginationValue(raw string, fallback, maximum int) (int, bool) {
	if raw == "" {
		return fallback, true
	}
	return decimalParameter(raw, 0, maximum)
}

func decimalParameter(raw string, minimum, maximum int) (int, bool) {
	if raw == "" {
		return 0, false
	}
	for _, character := range raw {
		if character < '0' || character > '9' {
			return 0, false
		}
	}
	value, err := strconv.Atoi(raw)
	return value, err == nil && value >= minimum && value <= maximum
}
func methodNotAllowed(writer http.ResponseWriter, allow string) {
	writer.Header().Set("Allow", allow)
	writeError(writer, http.StatusMethodNotAllowed, "method not allowed")
}
func writeError(writer http.ResponseWriter, status int, message string) {
	writeJSON(writer, status, map[string]string{"error": message})
}
func writeJSON(writer http.ResponseWriter, status int, value any) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(value)
}
func databaseError(writer http.ResponseWriter, err error) {
	if errors.Is(err, sql.ErrConnDone) || strings.Contains(err.Error(), "locked") || strings.Contains(err.Error(), "busy") {
		writeError(writer, http.StatusServiceUnavailable, "database busy")
		return
	}
	writeError(writer, http.StatusInternalServerError, "database error")
}
