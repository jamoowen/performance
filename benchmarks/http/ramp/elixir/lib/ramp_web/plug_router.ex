defmodule RampWeb.PlugRouter do
  @moduledoc false
  use Plug.Router

  plug(:match)
  plug(:dispatch)

  get("/healthz", do: RampWeb.Responses.send_json(conn, 200, %{status: "ok"}))
  get("/benchmark/info", do: RampWeb.Responses.send_json(conn, 200, Ramp.Database.metadata()))

  get "/benchmark/integrity" do
    [rows, total_stock, total_revisions] = Ramp.Database.integrity()

    RampWeb.Responses.send_json(conn, 200, %{
      rows: rows,
      totalStock: total_stock,
      totalRevisions: total_revisions
    })
  end

  get("/products", do: RampWeb.Responses.list_products(conn))
  get("/products/:id", do: RampWeb.Responses.product(conn, id))
  post("/products/:id/stock", do: RampWeb.Responses.stock(conn, id))
end
