# A spend alarm, because the failure mode of a personal AWS account is not the
# monthly bill you planned for but the one you did not notice. This stack should
# sit around $25 a month; anything approaching double that means something is
# running that nobody intended.
#
# Budgets is a global service. It is created through the same provider, and the
# notifications go to the same address as the collector alarms.
resource "aws_budgets_budget" "monthly" {
  name         = "ontime-sd-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Warn on the way up, on actual spend.
  dynamic "notification" {
    for_each = var.alarm_email == "" ? [] : [80, 100]

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alarm_email]
    }
  }

  # And once on the forecast, which is the one that catches a mistake early
  # rather than after the money is spent.
  dynamic "notification" {
    for_each = var.alarm_email == "" ? [] : [100]

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "FORECASTED"
      subscriber_email_addresses = [var.alarm_email]
    }
  }
}
