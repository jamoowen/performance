defmodule Ramp.MixProject do
  use Mix.Project

  def project do
    [
      app: :ramp,
      version: "0.1.0",
      elixir: "~> 1.20",
      start_permanent: Mix.env() == :prod,
      deps: deps(),
      releases: [
        ramp: [include_executables_for: [:unix], applications: [runtime_tools: :permanent]]
      ]
    ]
  end

  def application do
    [extra_applications: [:logger], mod: {Ramp.Application, []}]
  end

  defp deps do
    [
      {:bandit, "1.12.5"},
      {:credo, "~> 1.7", only: [:dev, :test], runtime: false},
      {:exqlite, "0.42.0"},
      {:jason, "1.4.5"},
      {:phoenix, "1.8.15"},
      {:plug, "1.20.3"}
    ]
  end
end
