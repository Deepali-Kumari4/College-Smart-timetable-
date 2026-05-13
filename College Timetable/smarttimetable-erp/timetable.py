from pulp import *
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.txt")
TIMETABLE_FILE = os.path.join(BASE_DIR, "timetable_output.txt")


def run():

    # -----------------------------
    # Collect output for students
    # -----------------------------
    output = []   # 🔥 NEW


    # -----------------------------
    # Read Teacher Data
    # -----------------------------

    courses = []
    teachers = {}
    students = {}
    preferences = {}
    targets = {}

    try:
        with open(DATA_FILE) as f:

            for line in f:

                if line.strip() == "":
                    continue

                parts = line.strip().split(",")

                c = parts[0]
                t = parts[1]
                s = int(parts[2])

                courses.append(c)
                teachers[c] = t
                students[c] = s

                # Optional target department (new format)
                start_index = 3
                if len(parts) >= 7 and ":" not in parts[3]:
                    targets[c] = parts[3] if parts[3] else "ALL"
                    start_index = 4
                else:
                    targets[c] = "ALL"

                # Preferences
                prefs = []
                for p in parts[start_index:]:
                    if p != "-:-":
                        d, sl = p.split(":")
                        prefs.append(f"{d}_{sl}")

                preferences[c] = prefs

    except:
        print("ERROR: data.txt missing or invalid")
        with open(TIMETABLE_FILE, "w") as f:
            f.write("")
        return False, "ERROR:data.txt missing or invalid"


    # -----------------------------
    # Admin Data
    # -----------------------------

    rooms = {
        "R1": 120,
        "R2": 100,
        "R3": 80,
        "R4": 65
    }

    slots = [
        "Mon_S1","Mon_S2","Mon_S3","Mon_S4",
        "Tue_S1","Tue_S2","Tue_S3","Tue_S4",
        "Wed_S1","Wed_S2","Wed_S3","Wed_S4",
        "Thu_S1","Thu_S2","Thu_S3","Thu_S4",
        "Fri_S1","Fri_S2","Fri_S3","Fri_S4"
    ]


    # -----------------------------
    # Priority (Senior > Junior)
    # -----------------------------

    priority = {
        "T1": 5,
        "T2": 15,
        "T3": 25
    }


    # -----------------------------
    # Weights
    # -----------------------------

    WEIGHT_PREF = 50
    WEIGHT_LATE = 10


    # -----------------------------
    # Model
    # -----------------------------

    model = LpProblem("Smart_Timetable", LpMinimize)


    # -----------------------------
    # Variables
    # -----------------------------

    x = LpVariable.dicts(
        "x",
        (courses, slots, rooms),
        cat="Binary"
    )


    # -----------------------------
    # Hard Constraints
    # -----------------------------

    # Each course fixed number of classes
    for c in courses:
        model += lpSum(
            x[c][s][r]
            for s in slots
            for r in rooms
        ) == len(preferences[c])

    # Teacher clash
    for s in slots:
        for t in set(teachers.values()):
            model += lpSum(
                x[c][s][r]
                for c in courses if teachers[c] == t
                for r in rooms
            ) <= 1

    # Room capacity
    for c in courses:
        for s in slots:
            for r in rooms:
                if students[c] > rooms[r]:
                    model += x[c][s][r] == 0

    # Room clash
    for s in slots:
        for r in rooms:
            model += lpSum(
                x[c][s][r] for c in courses
            ) <= 1

    # Student/department clash prevention:
    # For each department in each slot, allow at most one class that targets
    # that department. "ALL" is treated as common to every department.
    departments = sorted({targets[c] for c in courses if targets[c] != "ALL"})
    if not departments:
        departments = ["ALL"]

    for s in slots:
        # Clash among ALL-targeted common courses.
        model += lpSum(
            x[c][s][r]
            for c in courses if targets[c] == "ALL"
            for r in rooms
        ) <= 1

        # Clash per department, including common (ALL) courses.
        for dept in departments:
            model += lpSum(
                x[c][s][r]
                for c in courses if targets[c] in (dept, "ALL")
                for r in rooms
            ) <= 1

    # Room load balancing:
    # prevent all assignments from collapsing into a single room.
    total_required_classes = sum(len(preferences[c]) for c in courses)
    per_room_target = (total_required_classes + len(rooms) - 1) // len(rooms)
    max_per_room = per_room_target + 1
    for r in rooms:
        model += lpSum(
            x[c][s][r]
            for c in courses
            for s in slots
        ) <= max_per_room


    # -----------------------------
    # Soft Constraint Objective
    # -----------------------------

    cost_terms = []

    for c in courses:
        for s in slots:
            for r in rooms:

                cost = 0

                # Preference penalty
                if s not in preferences[c]:
                    cost += WEIGHT_PREF

                # Late slot penalty
                if s.endswith("S4"):
                    cost += WEIGHT_LATE

                # Teacher priority
                t = teachers[c]
                cost += priority.get(t, 20)

                cost_terms.append(cost * x[c][s][r])

    model += lpSum(cost_terms)


    # -----------------------------
    # Solve
    # -----------------------------

    status = model.solve()

    if LpStatus[status] != "Optimal":
        print("No feasible timetable found")
        with open(TIMETABLE_FILE, "w") as f:
            f.write("")
        return False, "No feasible timetable found. Check class sizes and preferences."


    # -----------------------------
    # Output
    # -----------------------------

    print("\n===== GENERATED TIMETABLE =====\n")

    violations = {}

    for c in courses:
        for s in slots:
            for r in rooms:

                if value(x[c][s][r]) == 1:

                    day, sl = s.split("_")

                    # 🔥 SAVE FOR STUDENT DASHBOARD
                    line = f"{day},{sl},{c},{r},{teachers[c]},{targets[c]}"
                    output.append(line)

                    # Admin terminal print
                    print(f"{c} -> {day} {sl} in {r}")

                    # Preference violation check
                    if s not in preferences[c]:
                        violations.setdefault(c, []).append(s)

    print("\n===============================\n")


    # -----------------------------
    # Save timetable to file
    # -----------------------------

    with open(TIMETABLE_FILE, "w") as f:
        for line in output:
            f.write(line + "\n")


    # -----------------------------
    # Notification System
    # -----------------------------

    if violations:
        print("WARNING: PREFERENCE VIOLATIONS FOUND\n")
        for c in violations:
            print(f"Course: {c}")
            print("Preferred:", preferences[c])
            print("Assigned :", violations[c])
            print("Suggested Alternatives:")
            for s in preferences[c]:
                print(" -", s)
            print()
    else:
        print("All preferences satisfied.\n")
    return True, "Timetable generated"


# Run
if __name__ == "__main__":
    run()
