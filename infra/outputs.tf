output "public_ip" {
  description = "Stable address of the collector host."
  value       = aws_eip.collector.public_ip
}

output "ssh" {
  description = "How to get a shell on it."
  value       = "ssh ec2-user@${aws_eip.collector.public_ip}"
}

output "psql_tunnel" {
  description = <<-EOT
    Postgres listens on localhost only. Open a tunnel, then connect to 5434 on
    the laptop to reach the cloud database without exposing it.
  EOT
  value       = "ssh -N -L 5434:localhost:5433 ec2-user@${aws_eip.collector.public_ip}"
}

output "alarm_topic_arn" {
  description = "SNS topic the staleness alarm publishes to."
  value       = aws_sns_topic.alerts.arn
}

output "monthly_cost_estimate" {
  description = "On-demand, us-west-2, before tax. Checked against the pricing page, not guessed."
  value = join(" ", [
    "~$23-26/mo on-demand:",
    "${var.instance_type} ~$12.26,",
    "${var.data_volume_gb} GB data gp3 ~$${var.data_volume_gb * 0.08},",
    "30 GB root gp3 ~$2.40,",
    "7 daily snapshots a few dollars,",
    "EIP free while attached.",
    "A 1 year Savings Plan takes roughly 30% off the instance."
  ])
}
