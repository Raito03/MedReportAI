"""CLI interface — run full pipeline from command line."""

import asyncio
import sys
import os
import io

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.orchestrator import run_pipeline


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m ui.cli <path-to-pdf>")
        sys.exit(1)

    pdf_path = sys.argv[1]
    if not os.path.exists(pdf_path):
        print(f"Error: File not found: {pdf_path}")
        sys.exit(1)

    result = asyncio.run(run_pipeline(pdf_path))

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)

    for exp in result["explanations"]:
        print(f"\n--- {exp['test_name']} ---")
        print(f"  {exp['explanation']}")
        print(f"  Questions for doctor:")
        for q in exp["doctor_questions"]:
            print(f"    - {q}")
        print(f"  Source: {exp['citation']}")

    if not result["verified"]:
        print(f"\n[!] VERIFICATION FAILED")
        for issue in result["issues"]:
            print(f"  - {issue}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
