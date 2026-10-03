defmodule RampWeb.ProductController do
  @moduledoc false
  use RampWeb, :controller

  def index(conn, _params), do: RampWeb.Responses.list_products(conn)
  def show(conn, %{"id" => id}), do: RampWeb.Responses.product(conn, id)
  def stock(conn, %{"id" => id}), do: RampWeb.Responses.stock(conn, id)
end
