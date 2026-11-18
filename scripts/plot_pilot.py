"""Plot observed simulation counts, never prescribed MockModel efficacy."""
import argparse
import csv
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter


def wilson(successes, total, z=1.96):
    if total <= 0 or not 0 <= successes <= total:
        raise ValueError("Invalid binomial count")
    p = successes / total
    denominator = 1 + z*z/total
    center = (p + z*z/(2*total)) / denominator
    radius = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / denominator
    return max(0, center-radius), min(1, center+radius)


def plot_summary(source, output):
    with Path(source).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows = [row for row in rows if row["subset"] == "without_controller_deviations"
            and row["requested_model"] == "gpt-6-luna"]
    fig, axes = plt.subplots(1, 2, figsize=(9, 4.6), sharey=True)
    for axis, variant, label, metric in (
        (axes[0], "clean", "Benign success (clean)", "benign_successes"),
        (axes[1], "injected", "Exfiltration success (injected)", "attack_successes"),
    ):
        selected = []
        for defense in ("D0", "D1"):
            matches = [row for row in rows if row["defense"] == defense and row["variant"] == variant]
            if len(matches) != 1:
                raise ValueError("Expected one row per requested-model/defense/variant")
            row = matches[0]
            selected.append((defense, int(row[metric]), int(row["n"])))
        for index, (defense, k, n) in enumerate(selected):
            low, high = wilson(k, n)
            p = k/n
            axis.bar(index, p, color=("#356a9a" if defense == "D0" else "#45815a"),
                     width=0.55, label=defense)
            axis.errorbar(index, p, yerr=[[p-low], [high-p]], color="#222222",
                          capsize=6, fmt="none", linewidth=1.4)
            axis.text(index, min(1.08, max(p, high)+0.035), f"{k}/{n}", ha="center", fontsize=11)
        axis.set_xticks([0, 1], ["D0", "D1"])
        axis.set_title(label, fontsize=11)
        axis.set_ylim(0, 1.15)
        axis.yaxis.set_major_formatter(PercentFormatter(1))
        axis.grid(axis="y", alpha=0.18)
        axis.set_axisbelow(True)
    fig.suptitle("Observed Luna-requested pilot simulations", fontsize=14)
    fig.text(0.5, 0.035, "Controller deviations excluded; unequal small denominators. 95% Wilson intervals are descriptive.",
             ha="center", fontsize=8)
    fig.text(0.5, 0.005, "Conversational transport, not native API system/tool roles. No general D1 efficacy claim.",
             ha="center", fontsize=8)
    fig.tight_layout(rect=[0, 0.08, 1, 0.94])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for suffix in ("png", "pdf"):
        fig.savefig(output / f"observed-luna-pilot.{suffix}", dpi=220, bbox_inches="tight")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plot_summary(args.summary, args.output)


if __name__ == "__main__":
    main()
