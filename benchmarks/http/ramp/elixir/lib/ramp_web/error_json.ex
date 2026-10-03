defmodule RampWeb.ErrorJSON do
  @moduledoc false
  def render(_template, _assigns), do: %{error: "internal server error"}
end
