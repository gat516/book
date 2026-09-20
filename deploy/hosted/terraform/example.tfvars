region             = "us-east-1"
name               = "private-books"
budget_email       = "replace@example.com"
monthly_budget_usd = 100
# Set false if you already configured an account-wide budget in the AWS console.
create_budget = true
# The budget is an alert, not a spending cap. Review terraform plan and AWS estimate.
