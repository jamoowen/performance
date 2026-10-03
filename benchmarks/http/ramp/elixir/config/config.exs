import Config

config :logger, level: :warning

config :ramp, RampWeb.Endpoint,
  url: [host: "localhost"],
  secret_key_base: "RAMP_SQLITE_V2_SECRET_KEY_BASE_FOR_BENCHMARK_ONLY_0123456789",
  code_reloader: false,
  debug_errors: false,
  render_errors: [formats: [json: RampWeb.ErrorJSON], layout: false]
