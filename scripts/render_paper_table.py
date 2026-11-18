"""Build a booktabs table from labeled CSV; never invent missing results."""
import argparse
import csv
from pathlib import Path

ESCAPES = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
           "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
           "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def escape(text):
    return "".join(ESCAPES.get(char, char) for char in str(text))


def build(source, output):
    with Path(source).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        needed = {"model", "evidence", "defense", "clean_n", "bts", "asr_exfil"}
        if not needed <= set(reader.fieldnames or []):
            raise ValueError("Missing table CSV columns")
        rows = list(reader)
    lines = [r"\begin{table}[ht]", r"\centering", r"\small",
             r"\begin{tabular}{llllrr}", r"\toprule",
             r"Model & Evidence & Defense & $n$ & BTS & ASR \\", r"\midrule"]
    for row in rows:
        n, bts, asr = int(row["clean_n"]), float(row["bts"]), float(row["asr_exfil"])
        if n < 0 or not (0 <= bts <= 1 and 0 <= asr <= 1):
            raise ValueError("Invalid count or rate")
        cells = [escape(row[key]) for key in ("model", "evidence", "defense")]
        cells += [str(n), f"{100*bts:.1f}\\%", f"{100*asr:.1f}\\%"]
        lines.append(" & ".join(cells) + r" \\")
    if not rows:
        lines.append(r"\multicolumn{6}{c}{No empirical results available; explicit placeholder.} \\")
    lines += [r"\bottomrule", r"\end{tabular}",
              r"\caption{Machine-generated from labeled CSV; evidence types must not be pooled.}",
              r"\end{table}"]
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == "__main__":
    main()
