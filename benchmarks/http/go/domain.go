package main

import (
	"context"
	"fmt"
)

var categories = []string{"books", "electronics", "home", "sports", "toys"}

type Product struct {
	ID         int      `json:"id"`
	Name       string   `json:"name"`
	Category   string   `json:"category"`
	PriceCents int      `json:"priceCents"`
	Stock      int      `json:"stock"`
	Tags       []string `json:"tags"`
}

type listResult struct {
	Products []Product
	Total    int
	Offset   int
	Limit    int
}

type categoryReport struct {
	Category            string `json:"category"`
	Count               int    `json:"count"`
	Stock               int    `json:"stock"`
	InventoryValueCents int    `json:"inventoryValueCents"`
}

type catalogResult struct {
	Categories               []categoryReport `json:"categories"`
	TotalStock               int              `json:"totalStock"`
	TotalInventoryValueCents int              `json:"totalInventoryValueCents"`
}

type eventResult struct {
	Counts map[string]int64 `json:"counts"`
	Values map[string]int64 `json:"values"`
}

type backend interface {
	product(context.Context, int) (Product, bool, error)
	list(context.Context, string, string, int, int) (listResult, error)
	catalog(context.Context) (catalogResult, error)
	recordEvents(context.Context, []event) error
	events(context.Context) (eventResult, error)
	close() error
}

func productFor(id int) Product {
	category := categories[(id-1)%len(categories)]
	secondTag := "standard"
	if id%3 == 0 {
		secondTag = "featured"
	}
	parityTag := "odd"
	if id%2 == 0 {
		parityTag = "even"
	}
	return Product{ID: id, Name: fmt.Sprintf("Product %05d", id), Category: category, PriceCents: 500 + (id*7919)%50000, Stock: (id * 37) % 201, Tags: []string{category, secondTag, parityTag}}
}
