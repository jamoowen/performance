defmodule Ramp.Database do
  @moduledoc false
  use GenServer

  alias Exqlite.Sqlite3

  @categories ["books", "electronics", "home", "outdoors", "clothing"]

  def start_link(_opts), do: GenServer.start_link(__MODULE__, [], name: __MODULE__)
  def detail(id), do: GenServer.call(__MODULE__, {:detail, id}, 6_000)
  def list(offset, limit), do: GenServer.call(__MODULE__, {:list, offset, limit}, 6_000)
  def stock(id, delta), do: GenServer.call(__MODULE__, {:stock, id, delta}, 6_000)
  def integrity, do: GenServer.call(__MODULE__, :integrity, 6_000)
  def metadata, do: GenServer.call(__MODULE__, :metadata, 6_000)
  def seed_count, do: :persistent_term.get({__MODULE__, :seed_count})

  @impl true
  def init([]) do
    seed_count = System.get_env("SEED_COUNT", "5000") |> String.to_integer()

    if seed_count < 100 or seed_count > 100_000 do
      raise "SEED_COUNT must be between 100 and 100000"
    end

    path = System.get_env("SQLITE_PATH", "/data/benchmark.sqlite")
    path |> Path.dirname() |> File.mkdir_p!()
    :persistent_term.put({__MODULE__, :seed_count}, seed_count)
    {:ok, connection} = Sqlite3.open(path)
    initialize!(connection, seed_count)

    statements = %{
      detail:
        prepare!(
          connection,
          "SELECT id, name, category, price_cents, stock, revision FROM products WHERE id = ?"
        ),
      list:
        prepare!(
          connection,
          "SELECT id, name, category, price_cents, stock, revision FROM products ORDER BY id LIMIT ? OFFSET ?"
        ),
      stock:
        prepare!(
          connection,
          "UPDATE products SET stock = stock + ?, revision = revision + 1 WHERE id = ? RETURNING id, stock, revision"
        ),
      integrity:
        prepare!(
          connection,
          "SELECT COUNT(*), COALESCE(SUM(stock), 0), COALESCE(SUM(revision), 0) FROM products"
        ),
      sqlite_version: prepare!(connection, "SELECT sqlite_version()")
    }

    {:ok,
     %{
       connection: connection,
       seed_count: seed_count,
       statements: statements,
       pragmas: pragmas(connection),
       sqlite_compile_options: compile_options(connection)
     }}
  end

  @impl true
  def handle_call({:detail, id}, _from, state) do
    {:reply, one(state.connection, state.statements.detail, [id]), state}
  end

  def handle_call({:list, offset, limit}, _from, state) do
    {:reply, {all(state.connection, state.statements.list, [limit, offset]), state.seed_count},
     state}
  end

  def handle_call({:stock, id, delta}, _from, state) do
    {:reply, one(state.connection, state.statements.stock, [delta, id]), state}
  end

  def handle_call(:integrity, _from, state) do
    {:reply, one(state.connection, state.statements.integrity, []), state}
  end

  def handle_call(:metadata, _from, state) do
    sqlite_version = one(state.connection, state.statements.sqlite_version, []) |> List.first()

    {:reply,
     %{
       experiment: "sqlite-ramp-v2",
       runtime: "elixir",
       framework: System.get_env("FRAMEWORK", "phoenix"),
       runtimeVersion: System.version(),
       frameworkVersion: framework_version(),
       driver: "exqlite",
       driverVersion: Application.spec(:exqlite, :vsn) |> to_string(),
       sqliteVersion: sqlite_version,
       compileOptions: state.sqlite_compile_options,
       seedCount: state.seed_count,
       workers: 1,
       pragmas: state.pragmas,
       otpRelease: to_string(:erlang.system_info(:otp_release)),
       schedulerFlags: System.get_env("ERL_FLAGS", ""),
       schedulers: :erlang.system_info(:schedulers),
       schedulersOnline: :erlang.system_info(:schedulers_online),
       dirtyCpuSchedulers: :erlang.system_info(:dirty_cpu_schedulers),
       dirtyCpuSchedulersOnline: :erlang.system_info(:dirty_cpu_schedulers_online),
       dirtyIoSchedulers: :erlang.system_info(:dirty_io_schedulers)
     }, state}
  end

  @impl true
  def terminate(_reason, state) do
    Enum.each(state.statements, fn {_name, statement} ->
      Sqlite3.release(state.connection, statement)
    end)

    Sqlite3.close(state.connection)
  end

  defp initialize!(connection, seed_count) do
    Enum.each(
      [
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA foreign_keys=ON",
        "PRAGMA busy_timeout=5000",
        "PRAGMA cache_size=-2000",
        "PRAGMA wal_autocheckpoint=1000",
        "PRAGMA temp_store=MEMORY"
      ],
      &execute!(connection, &1)
    )

    case query_all(
           connection,
           "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'products'"
         ) do
      [] -> create_and_seed!(connection, seed_count)
      _rows -> verify_seed_count!(connection, seed_count)
    end
  end

  defp create_and_seed!(connection, seed_count) do
    execute!(
      connection,
      "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price_cents INTEGER NOT NULL, stock INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0)"
    )

    statement =
      prepare!(
        connection,
        "INSERT INTO products(id, name, category, price_cents, stock, revision) VALUES (?, ?, ?, ?, ?, 0)"
      )

    execute!(connection, "BEGIN")

    try do
      Enum.each(1..seed_count, fn id ->
        :ok =
          Sqlite3.bind(statement, [
            id,
            "Product#{String.pad_leading(Integer.to_string(id), 5, "0")}",
            Enum.at(@categories, rem(id - 1, 5)),
            500 + rem(id * 7919, 50_000),
            rem(id * 37, 201)
          ])

        :done = Sqlite3.step(connection, statement)
        :ok = Sqlite3.reset(statement)
      end)

      execute!(connection, "COMMIT")
    rescue
      error ->
        execute!(connection, "ROLLBACK")
        reraise error, __STACKTRACE__
    after
      Sqlite3.release(connection, statement)
    end
  end

  defp verify_seed_count!(connection, seed_count) do
    [[rows]] = query_all(connection, "SELECT COUNT(*) FROM products")

    if rows != seed_count do
      raise "existing products row count #{rows} does not match SEED_COUNT #{seed_count}"
    end
  end

  defp pragmas(connection) do
    %{
      journal_mode: pragma(connection, "journal_mode"),
      synchronous: pragma(connection, "synchronous"),
      foreign_keys: pragma(connection, "foreign_keys"),
      busy_timeout: pragma(connection, "busy_timeout"),
      cache_size: pragma(connection, "cache_size"),
      wal_autocheckpoint: pragma(connection, "wal_autocheckpoint"),
      temp_store: pragma(connection, "temp_store")
    }
  end

  defp pragma(connection, name) do
    [[value]] = query_all(connection, "PRAGMA #{name}")
    value
  end

  defp compile_options(connection) do
    connection
    |> query_all("PRAGMA compile_options")
    |> Enum.map(fn [option] -> option end)
  end

  defp prepare!(connection, sql) do
    {:ok, statement} = Sqlite3.prepare(connection, sql)
    statement
  end

  defp execute!(connection, sql) do
    case Sqlite3.execute(connection, sql) do
      :ok -> :ok
      {:ok, _} -> :ok
      other -> raise "SQLite execution failed: #{inspect(other)}"
    end
  end

  defp one(connection, statement, values) do
    :ok = Sqlite3.bind(statement, values)

    try do
      case Sqlite3.step(connection, statement) do
        {:row, row} -> row
        :done -> nil
      end
    after
      :ok = Sqlite3.reset(statement)
    end
  end

  defp all(connection, statement, values) do
    :ok = Sqlite3.bind(statement, values)

    try do
      fetch_rows(connection, statement, [])
    after
      :ok = Sqlite3.reset(statement)
    end
  end

  defp fetch_rows(connection, statement, rows) do
    case Sqlite3.step(connection, statement) do
      {:row, row} -> fetch_rows(connection, statement, [row | rows])
      :done -> Enum.reverse(rows)
    end
  end

  defp framework_version do
    case System.get_env("FRAMEWORK", "phoenix") do
      "phoenix" -> Application.spec(:phoenix, :vsn) |> to_string()
      "plug" -> Application.spec(:plug, :vsn) |> to_string()
    end
  end

  defp query_all(connection, sql) do
    statement = prepare!(connection, sql)

    try do
      all(connection, statement, [])
    after
      Sqlite3.release(connection, statement)
    end
  end
end
