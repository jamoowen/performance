defmodule RampWeb.Router do
  @moduledoc false
  use RampWeb, :router

  scope "/" do
    get("/healthz", RampWeb.BenchmarkController, :healthz)
    get("/benchmark/info", RampWeb.BenchmarkController, :info)
    get("/benchmark/integrity", RampWeb.BenchmarkController, :integrity)
    get("/products", RampWeb.ProductController, :index)
    get("/products/:id", RampWeb.ProductController, :show)
    post("/products/:id/stock", RampWeb.ProductController, :stock)
  end
end
