package main

import (
	"context"
	"strings"
	"sync"
)

// memoryStore scans its seeded catalog for every list and report. Event totals are
// the only mutable state and are updated while holding one lock so batch writes remain atomic.
type memoryStore struct {
	seedCount int
	products  []Product
	mu        sync.RWMutex
	eventsBy  map[int64]map[string]eventTotal
}

type eventTotal struct {
	count int64
	value int64
}

func newMemoryStore(seedCount int) *memoryStore {
	products := make([]Product, seedCount)
	for id := 1; id <= seedCount; id++ {
		products[id-1] = productFor(id)
	}
	return &memoryStore{seedCount: seedCount, products: products, eventsBy: make(map[int64]map[string]eventTotal)}
}

func (store *memoryStore) close() error { return nil }

func (store *memoryStore) product(_ context.Context, id int) (Product, bool, error) {
	if id < 1 || id > store.seedCount {
		return Product{}, false, nil
	}
	return store.products[id-1], true, nil
}

func (store *memoryStore) list(_ context.Context, category, search string, offset, limit int) (listResult, error) {
	products := make([]Product, 0, limit)
	total := 0
	for _, product := range store.products {
		if category != "" && product.Category != category {
			continue
		}
		if search != "" && !strings.Contains(strings.ToLower(product.Name), search) {
			continue
		}
		if total >= offset && len(products) < limit {
			products = append(products, product)
		}
		total++
	}
	if offset > total {
		offset = total
		products = products[:0]
	}
	return listResult{Products: products, Total: total, Offset: offset, Limit: limit}, nil
}

func (store *memoryStore) catalog(_ context.Context) (catalogResult, error) {
	result := catalogResult{Categories: make([]categoryReport, len(categories))}
	byCategory := make(map[string]int, len(categories))
	for index, category := range categories {
		result.Categories[index].Category = category
		byCategory[category] = index
	}
	for _, product := range store.products {
		report := &result.Categories[byCategory[product.Category]]
		report.Count++
		report.Stock += product.Stock
		report.InventoryValueCents += product.Stock * product.PriceCents
	}
	for _, report := range result.Categories {
		result.TotalStock += report.Stock
		result.TotalInventoryValueCents += report.InventoryValueCents
	}
	return result, nil
}

func (store *memoryStore) recordEvents(_ context.Context, events []event) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	for _, item := range events {
		byType := store.eventsBy[*item.UserID]
		if byType == nil {
			byType = make(map[string]eventTotal)
			store.eventsBy[*item.UserID] = byType
		}
		total := byType[item.Type]
		total.count++
		total.value += *item.Value
		byType[item.Type] = total
	}
	return nil
}

func (store *memoryStore) events(_ context.Context) (eventResult, error) {
	result := eventResult{Counts: map[string]int64{"view": 0, "click": 0, "purchase": 0}, Values: map[string]int64{"view": 0, "click": 0, "purchase": 0}}
	store.mu.RLock()
	defer store.mu.RUnlock()
	for _, byType := range store.eventsBy {
		for eventType, total := range byType {
			result.Counts[eventType] += total.count
			result.Values[eventType] += total.value
		}
	}
	return result, nil
}
