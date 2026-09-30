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
	"strconv"
	"strings"
)

const maxBodyBytes = 1 << 20

type apiServer struct {
	store     *Store
	seedCount int
}

func newServer(store *Store, seedCount int) http.Handler {
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
	context := request.Context()
	var total int
	if err := server.store.listCount.QueryRowContext(context, category, category, search, search).Scan(&total); err != nil {
		databaseError(writer, err)
		return
	}
	if offset > total {
		offset = total
	}
	rows, err := server.store.listRows.QueryContext(context, category, category, search, search, limit, offset)
	if err != nil {
		databaseError(writer, err)
		return
	}
	defer func() { _ = rows.Close() }()
	products := make([]Product, 0)
	for rows.Next() {
		product, err := scanProduct(rows)
		if err != nil {
			databaseError(writer, err)
			return
		}
		products = append(products, product)
	}
	if err := rows.Err(); err != nil {
		databaseError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, struct {
		Products []Product `json:"products"`
		Total    int       `json:"total"`
		Offset   int       `json:"offset"`
		Limit    int       `json:"limit"`
	}{products, total, offset, limit})
}

func (server apiServer) catalogReport(writer http.ResponseWriter, request *http.Request) {
	type categoryReport struct {
		Category            string `json:"category"`
		Count               int    `json:"count"`
		Stock               int    `json:"stock"`
		InventoryValueCents int    `json:"inventoryValueCents"`
	}
	reports := make([]categoryReport, len(categories))
	for i, category := range categories {
		reports[i].Category = category
	}
	totalStock, totalValue := 0, 0
	rows, err := server.store.catalogReport.QueryContext(request.Context())
	if err != nil {
		databaseError(writer, err)
		return
	}
	defer func() { _ = rows.Close() }()
	for rows.Next() {
		var category string
		var count, stock, value int
		if err := rows.Scan(&category, &count, &stock, &value); err != nil {
			databaseError(writer, err)
			return
		}
		for index := range reports {
			if reports[index].Category == category {
				reports[index].Count, reports[index].Stock, reports[index].InventoryValueCents = count, stock, value
				totalStock += stock
				totalValue += value
			}
		}
	}
	if err := rows.Err(); err != nil {
		databaseError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, struct {
		Categories               []categoryReport `json:"categories"`
		TotalStock               int              `json:"totalStock"`
		TotalInventoryValueCents int              `json:"totalInventoryValueCents"`
	}{reports, totalStock, totalValue})
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
	tx, err := server.store.db.BeginTx(request.Context(), nil)
	if err != nil {
		databaseError(writer, err)
		return
	}
	defer func() { _ = tx.Rollback() }()
	statement := tx.StmtContext(request.Context(), server.store.eventUpsert)
	defer func() { _ = statement.Close() }()
	for _, item := range input.Events {
		if _, err := statement.ExecContext(request.Context(), *item.UserID, item.Type, *item.Value); err != nil {
			databaseError(writer, err)
			return
		}
	}
	if err := tx.Commit(); err != nil {
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
	counts := map[string]int64{"view": 0, "click": 0, "purchase": 0}
	values := map[string]int64{"view": 0, "click": 0, "purchase": 0}
	rows, err := server.store.eventsReport.QueryContext(request.Context())
	if err != nil {
		databaseError(writer, err)
		return
	}
	defer func() { _ = rows.Close() }()
	for rows.Next() {
		var eventType string
		var count, value int64
		if err := rows.Scan(&eventType, &count, &value); err != nil {
			databaseError(writer, err)
			return
		}
		counts[eventType], values[eventType] = count, value
	}
	if err := rows.Err(); err != nil {
		databaseError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, struct {
		Counts map[string]int64 `json:"counts"`
		Values map[string]int64 `json:"values"`
	}{counts, values})
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
