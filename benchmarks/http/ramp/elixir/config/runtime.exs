import Config

port = System.get_env("PORT", "8080") |> String.to_integer()

config :ramp, RampWeb.Endpoint,
  server: System.get_env("FRAMEWORK", "phoenix") == "phoenix",
  http: [
    ip: {0, 0, 0, 0},
    port: port,
    startup_log: false,
    http_options: [compress: false, log_protocol_errors: false, log_client_closures: false]
  ],
  adapter: Bandit.PhoenixAdapter
