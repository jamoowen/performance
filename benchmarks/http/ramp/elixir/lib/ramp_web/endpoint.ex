defmodule RampWeb.Endpoint do
  @moduledoc false
  use Phoenix.Endpoint, otp_app: :ramp

  plug(RampWeb.Router)
end
