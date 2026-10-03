# The alarm lives in CloudWatch rather than on the box, so it still fires when
# the box is the thing that has failed. That is the whole reason the local
# watchdog (ADR-0040) is not simply copied across: a watchdog running on the
# host it watches cannot report that the host is gone.

resource "aws_sns_topic" "alerts" {
  name = "ontime-sd-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alarm_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

resource "aws_cloudwatch_metric_alarm" "collection_stale" {
  alarm_name        = "ontime-sd-collection-stale"
  alarm_description = "No successful poll recently. Realtime data cannot be backfilled, so this is permanent loss while it lasts."

  namespace   = "OnTimeSD"
  metric_name = "SecondsSinceLastPoll"
  statistic   = "Maximum"
  period      = 60

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.staleness_alarm_seconds
  evaluation_periods  = 2

  # Missing data means the metric stopped being published, which means the
  # instance or its timer is down. That is an outage, not an unknown.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# A second alarm on the instance itself, because a failed status check produces
# no application metrics at all and would otherwise read as the alarm above.
resource "aws_cloudwatch_metric_alarm" "instance_unhealthy" {
  alarm_name        = "ontime-sd-instance-unhealthy"
  alarm_description = "EC2 status check failing on the collector host."

  namespace   = "AWS/EC2"
  metric_name = "StatusCheckFailed"
  statistic   = "Maximum"
  period      = 60

  dimensions = { InstanceId = aws_instance.collector.id }

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 2

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# Disk filling is the slow failure this design has: Postgres on one volume with
# months of runway is fine until it is not, and a full volume stops collection.
resource "aws_cloudwatch_metric_alarm" "data_volume_low" {
  alarm_name        = "ontime-sd-data-volume-low"
  alarm_description = "Data volume read ops have stopped, which usually means the filesystem is full or detached."

  namespace   = "AWS/EBS"
  metric_name = "VolumeIdleTime"
  statistic   = "Average"
  period      = 300

  dimensions = { VolumeId = aws_ebs_volume.data.id }

  comparison_operator = "GreaterThanThreshold"
  threshold           = 295
  evaluation_periods  = 6

  alarm_actions = [aws_sns_topic.alerts.arn]
}
