defmodule RampWeb.BenchmarkController do
  @moduledoc false
  use RampWeb, :controller

  def healthz(conn, _params), do: RampWeb.Responses.send_json(conn, 200, %{status: "ok"})

  def info(conn, _params) do
    RampWeb.Responses.send_json(conn, 200, Ramp.Database.metadata())
  end

  def integrity(conn, _params) do
    [rows, total_stock, total_revisions] = Ramp.Database.integrity()

    RampWeb.Responses.send_json(conn, 200, %{
      rows: rows,
      totalStock: total_stock,
      totalRevisions: total_revisions
    })
  end
end
