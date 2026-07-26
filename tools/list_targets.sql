.mode column
.headers on
.width 32 8 12 10 40
SELECT name, staff, printf('%.0f млн', revenue/1e6) AS revenue,
       printf('%.1f', revenue/staff/1e6) AS per_emp,
       printf('%+.0f%%', revenue_change_pct) AS dynamics
FROM companies WHERE icp_status='passed' ORDER BY revenue_change_pct;
