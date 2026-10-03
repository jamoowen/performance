defmodule RampWeb.Responses do
  @moduledoc false
  import Plug.Conn

  @max_safe_integer 9_007_199_254_740_991
  @body_limit 65_536

  def send_json(conn, status, value, headers \\ []) do
    body = Jason.encode!(value)
    send_encoded_json(conn, status, body, headers)
  end

  defp send_encoded_json(conn, status, body, headers) do
    conn
    |> put_resp_content_type("application/json")
    |> then(fn response ->
      Enum.reduce(headers, response, fn {key, value}, acc -> put_resp_header(acc, key, value) end)
    end)
    |> send_resp(status, body)
  end

  def list_products(conn) do
    case pagination(conn.query_string) do
      {:ok, offset, limit} -> service_response(conn, fn -> list_response(offset, limit) end)
      {:error, status, message} -> error(conn, status, message)
    end
  end

  def product(conn, raw_id) do
    case identifier(raw_id) do
      {:ok, id} -> service_response(conn, fn -> detail_response(id) end)
      {:error, status, message} -> error(conn, status, message)
    end
  end

  def stock(conn, raw_id) do
    with {:ok, id} <- identifier(raw_id),
         {:ok, body, conn} <- read_limited_body(conn),
         {:ok, delta} <- delta(content_type(conn), body) do
      service_response(conn, fn -> stock_response(id, delta) end)
    else
      {:error, status, message} -> error(conn, status, message)
    end
  end

  defp service_response(conn, operation) do
    service_started = System.monotonic_time(:native)
    Process.put(:ramp_db_timing, nil)

    case operation.() do
      {:error, status, message} ->
        error(conn, status, message)

      value ->
        body = Jason.encode!(value)
        {db_started, db_finished} = Process.get(:ramp_db_timing)
        service_ms = elapsed_ms(service_started, System.monotonic_time(:native))
        db_ms = elapsed_ms(db_started, db_finished)

        send_encoded_json(conn, 200, body, [
          {"server-timing", "service;dur=#{format_ms(service_ms)}, db;dur=#{format_ms(db_ms)}"}
        ])
    end
  end

  defp timed_database(operation) do
    started = System.monotonic_time(:native)
    result = operation.()
    finished = System.monotonic_time(:native)
    Process.put(:ramp_db_timing, {started, finished})
    result
  end

  defp list_response(offset, limit) do
    {rows, total} = timed_database(fn -> Ramp.Database.list(offset, limit) end)

    %{
      products: Enum.map(rows, &product/1),
      total: total,
      offset: offset,
      limit: limit
    }
  end

  defp detail_response(id) do
    case timed_database(fn -> Ramp.Database.detail(id) end) do
      nil -> {:error, 404, "product not found"}
      row -> product(row)
    end
  end

  defp stock_response(id, delta) do
    case timed_database(fn -> Ramp.Database.stock(id, delta) end) do
      nil -> {:error, 404, "product not found"}
      [updated_id, stock, revision] -> %{id: updated_id, stock: stock, revision: revision}
    end
  end

  defp error(conn, status, message), do: send_json(conn, status, %{error: message})

  defp read_limited_body(conn) do
    case read_body(conn, length: @body_limit, read_length: @body_limit) do
      {:ok, body, next_conn} when byte_size(body) <= @body_limit -> {:ok, body, next_conn}
      {:more, _body, _next_conn} -> {:error, 413, "request body is too large"}
      _ -> {:error, 413, "request body is too large"}
    end
  end

  defp content_type(conn) do
    conn
    |> get_req_header("content-type")
    |> List.first()
  end

  defp delta(content_type, body) do
    media_type =
      content_type
      |> to_string()
      |> String.split(";", parts: 2)
      |> List.first()
      |> String.trim()
      |> String.downcase()

    if media_type == "application/json",
      do: decode_delta(body),
      else: {:error, 415, "content type must be application/json"}
  end

  defp decode_delta(body) do
    case Jason.decode(body) do
      {:ok, %{"delta" => value} = map}
      when map_size(map) == 1 and is_integer(value) and value >= -100 and value <= 100 ->
        {:ok, value}

      {:ok, _} ->
        {:error, 400, "body must contain only integer delta"}

      {:error, _} ->
        {:error, 400, "body must be valid JSON"}
    end
  end

  defp pagination(query_string) do
    values = URI.decode_query(query_string)
    pairs = if query_string == "", do: [], else: String.split(query_string, "&", trim: false)

    valid_pairs =
      Enum.all?(pairs, fn pair ->
        case String.split(pair, "=", parts: 2) do
          [raw_name, _] -> URI.decode_www_form(raw_name) in ["offset", "limit"]
          [raw_name] -> URI.decode_www_form(raw_name) in ["offset", "limit"]
        end
      end)

    names =
      Enum.map(pairs, fn pair ->
        pair |> String.split("=", parts: 2) |> hd() |> URI.decode_www_form()
      end)

    if valid_pairs and length(names) == length(Enum.uniq(names)) do
      with {:ok, offset} <-
             non_negative(Map.get(values, "offset", "0"), "offset", Ramp.Database.seed_count()),
           {:ok, limit} <- non_negative(Map.get(values, "limit", "20"), "limit", 100),
           true <- limit > 0 do
        {:ok, offset, limit}
      else
        false -> {:error, 400, "limit is out of range"}
        {:error, _, _} = error -> error
      end
    else
      {:error, 400, "query must contain only one offset and one limit"}
    end
  end

  defp identifier(value) do
    case Integer.parse(value) do
      {id, ""} ->
        if String.match?(value, ~r/^[0-9]+$/) and id > 0 and id <= @max_safe_integer do
          {:ok, id}
        else
          {:error, 400, "id must be a positive integer"}
        end

      _ ->
        {:error, 400, "id must be a positive integer"}
    end
  end

  defp non_negative(value, name, maximum) do
    case Integer.parse(value) do
      {number, ""} ->
        if String.match?(value, ~r/^[0-9]+$/) and number >= 0 and number <= maximum do
          {:ok, number}
        else
          {:error, 400, "#{name} is out of range"}
        end

      _ ->
        {:error, 400, "#{name} must be an integer"}
    end
  end

  defp product([id, name, category, price_cents, stock, revision]) do
    %{
      id: id,
      name: name,
      category: category,
      priceCents: price_cents,
      stock: stock,
      revision: revision
    }
  end

  defp elapsed_ms(started, finished),
    do: System.convert_time_unit(finished - started, :native, :microsecond) / 1_000

  defp format_ms(value), do: :erlang.float_to_binary(value, decimals: 3)
end
