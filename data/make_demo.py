"""Generate the HR demo dataset. Deterministic (seeded).

Join keys are deliberately named differently per file — employee_id / emp_id / staff_id —
so join detection is demonstrated, not assumed.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

random.seed(42)
OUT = Path(__file__).parent / "demo"
OUT.mkdir(exist_ok=True)

N_EMP = 120
DEPTS = ["Engineering", "Sales", "HR", "Finance", "Operations"]
LOCS = ["Hyderabad", "Bengaluru", "Mumbai", "Singapore", "Dubai"]
FIRST = ["Aarav", "Priya", "Rahul", "Sneha", "Vikram", "Ananya", "Karan", "Meera", "Arjun", "Divya",
         "Wei", "Fatima", "Omar", "Li", "Sara", "Ravi", "Nisha", "Dev", "Isha", "Tariq"]
LAST = ["Sharma", "Reddy", "Patel", "Nair", "Khan", "Iyer", "Tan", "Rao", "Menon", "Gupta"]

# --- employees.csv ------------------------------------------------------------
employees = []
for i in range(1, N_EMP + 1):
    dept = random.choice(DEPTS)
    employees.append({
        "employee_id": f"E{i:04d}",
        "name": f"{random.choice(FIRST)} {random.choice(LAST)}",
        "department": dept,
        "location": random.choice(LOCS),
        "join_date": date(2019, 1, 1) + timedelta(days=random.randint(0, 2000)),
        "grade": random.choice(["L1", "L2", "L3", "L4"]),
        "manager_id": f"E{random.randint(1, 10):04d}" if i > 10 else None,
    })
pd.DataFrame(employees).to_csv(OUT / "employees.csv", index=False)

# --- payroll.csv (monthly, 2025) ---------------------------------------------
base_salary = {e["employee_id"]: random.randint(40_000, 250_000) for e in employees}
payroll = []
for month in range(1, 13):
    for e in employees:
        if random.random() < 0.03:  # a few missing months
            continue
        base = base_salary[e["employee_id"]]
        payroll.append({
            "staff_id": e["employee_id"],
            "pay_month": date(2025, month, 1),
            "base_salary": base,
            "bonus": random.choice([0, 0, 0, round(base * random.uniform(0.05, 0.2))]),
            "deductions": round(base * random.uniform(0.08, 0.15)),
        })
pd.DataFrame(payroll).to_csv(OUT / "payroll.csv", index=False)

# --- attendance.xlsx (daily, Q1-Q2 2025, working days) -----------------------
attendance = []
d = date(2025, 1, 1)
while d <= date(2025, 6, 30):
    if d.weekday() < 5:
        for e in employees:
            r = random.random()
            status = "Present" if r < 0.85 else ("Leave" if r < 0.95 else "WFH")
            attendance.append({
                "emp_id": e["employee_id"],
                "date": d,
                "status": status,
                "hours": round(random.uniform(7, 10), 1) if status != "Leave" else 0.0,
            })
    d += timedelta(days=1)
pd.DataFrame(attendance).to_excel(OUT / "attendance.xlsx", index=False)

print(f"employees={len(employees)} payroll={len(payroll)} attendance={len(attendance)} -> {OUT}")
