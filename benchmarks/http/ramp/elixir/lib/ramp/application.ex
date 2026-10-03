defmodule Ramp.Application do
  @moduledoc false
  use Application

  @impl true
  def start(_type, _args) do
    framework = System.get_env("FRAMEWORK", "phoenix")
    port = System.get_env("PORT", "8080") |> String.to_integer()

    children =
      [Ramp.Database] ++
        case framework do
          "phoenix" ->
            [RampWeb.Endpoint]

          "plug" ->
            [
              {Bandit,
               plug: RampWeb.PlugRouter,
               ip: {0, 0, 0, 0},
               port: port,
               startup_log: false,
               http_options: [
                 compress: false,
                 log_protocol_errors: false,
                 log_client_closures: false
               ]}
            ]

          _ ->
            raise "FRAMEWORK must be phoenix or plug for the Elixir ramp image"
        end

    Supervisor.start_link(children, strategy: :one_for_one, name: Ramp.Supervisor)
  end
end
