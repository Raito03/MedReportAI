"""Streamlit UI — upload a lab report PDF, get explanations."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
from pipeline.orchestrator import run_pipeline
from tools.pdf_extractor import PdfExtractionError


def main():
    st.set_page_config(page_title="Lab Report Explainer", page_icon="🔬")
    st.title("🔬 Lab Report Explainer")
    st.markdown(
        "Upload a lab report PDF. The AI pipeline extracts values, checks "
        "reference ranges, and generates patient-friendly explanations."
    )

    uploaded_file = st.file_uploader("Upload PDF", type=["pdf"])

    if uploaded_file:
        temp_path = f"_temp_{uploaded_file.name}"
        with open(temp_path, "wb") as f:
            f.write(uploaded_file.read())

        with st.spinner("Analyzing lab report..."):
            try:
                result = asyncio.run(run_pipeline(temp_path))
            except PdfExtractionError as exc:
                # Controlled failure (P0-T3): show a clear error instead of
                # crashing the app for corrupt/blank/image-only PDFs.
                os.remove(temp_path)
                st.error(f"PDF extraction failed: {exc}")
                return

        os.remove(temp_path)

        if result["verified"]:
            st.success("✓ Verified — all explanations passed safety checks")
        else:
            st.warning("⚠ Some explanations may need review")
            for issue in result["issues"]:
                st.error(issue)

        for exp in result["explanations"]:
            with st.expander(f"📋 {exp['test_name']}", expanded=True):
                st.write(exp["explanation"])
                st.markdown("**Questions for your doctor:**")
                for q in exp["doctor_questions"]:
                    st.markdown(f"- {q}")
                st.caption(f"Source: {exp['citation']}")


if __name__ == "__main__":
    main()
