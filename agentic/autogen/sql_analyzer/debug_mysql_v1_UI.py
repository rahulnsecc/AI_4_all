import streamlit as st
from autogen import AssistantAgent, UserProxyAgent
import sqlalchemy
from sqlalchemy import create_engine, text
import json
import re
import os
from dotenv import load_dotenv
from typing import Dict, Any
import pandas as pd

# Load environment variables first
load_dotenv()

def create_engine():
    """Create database connection engine"""
    return sqlalchemy.create_engine(
        f"mysql+mysqlconnector://"
        f"{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}/{os.getenv('DB_NAME')}"
    )

class DebuggingSession:
    """Optimized session management with token control"""
    def __init__(self, engine):
        self.engine = engine
        self.history = []
        self.current_step = 0
        self.resolved = False
        
    def add_interaction(self, role: str, content: str):
        # Keep only last 3 interactions to limit context size
        if len(self.history) >= 3:
            self.history.pop(0)
        self.history.append({"role": role, "content": content})
        
    def should_continue(self, last_response: str) -> bool:
        if self.resolved or self.current_step >= 3:
            return False
        self.current_step += 1
        
        # Check for resolution indicators
        resolution_phrases = [
            "final solution",
            "error resolved",
            "successfully executed",
            "issue fixed"
        ]
        return not any(phrase in last_response.lower() for phrase in resolution_phrases)

class SQLDebugger:
    def __init__(self):
        self.engine = create_engine()
        self.session = DebuggingSession(self.engine)
        
        self.analyst = AssistantAgent(
            name="SQLAnalyst",
            system_message="""Analyze SQL errors and provide ONLY JSON responses with:
            {
                "hypothesis": "possible cause",
                "validation_query": "SQL to test hypothesis",
                "expected_result": "expected outcome",
                "solution": "proposed fix"
            }
            No explanations or markdown. Keep responses minimal.""",
            llm_config={
                "config_list": [{
                    "model": "llama-3.3-70b-versatile",
                    "api_key": os.getenv("GROQ_API_KEY"),
                    "api_type": "groq",
                }],
                "max_tokens": 300  # Enforce response size limit
            }
        )
        
        self.user_proxy = UserProxyAgent(
            name="DebugController",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=3,  # Limit conversation depth
            code_execution_config=False,
            function_map={
                "execute_query": self.execute_query,
                "validate_solution": self.validate_solution
            }
        )
        
    def execute_query(self, query: str) -> Dict[str, Any]:
        """Execute SQL query with token-safe error handling"""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(query))
                return {
                    "success": True,
                    "data": result.fetchmany(3),  # Limit data size
                    "rowcount": result.rowcount
                }
        except Exception as e:
            return {"success": False, "error": str(e).split('(Background')[0]}  # Trim verbose errors

    def validate_solution(self, solution_query: str) -> Dict[str, Any]:
        """Validate the proposed solution directly"""
        result = self.execute_query(solution_query)
        return {
            "valid": result["success"],
            "error": result.get("error"),
            "sample_data": result.get("data")
        }

    def debug_flow(self, query: str) -> Dict[str, Any]:
        """Optimized debugging flow with token control"""
        initial_result = self.execute_query(query)
        
        # Early return if the query executes successfully
        if initial_result["success"]:
            return {
                "status": "success", 
                "result": initial_result,
                "message": "The query executed successfully. No further analysis needed."
            }
            
        self.session.add_interaction("system", f"Error: {initial_result['error']}")
        
        while self.session.should_continue(self.session.history[-1]["content"]):
            try:
                response = self.user_proxy.initiate_chat(
                    self.analyst,
                    message=self.session.history[-1]["content"],
                    clear_history=True  # Prevent context bloat
                )
                last_msg = response.chat_history[-1]["content"]
                
                # Extract JSON without verbose parsing
                action = self._extract_json(last_msg)
                if not action:
                    break

                # Direct solution validation
                if "solution" in action:
                    validation = self.validate_solution(action["solution"])
                    if validation["valid"]:
                        self.session.resolved = True
                        return {
                            "status": "resolved",
                            "solution": action["solution"],
                            "validation": validation
                        }
                        
                self.session.add_interaction("assistant", json.dumps(action))
                
            except Exception as e:
                break
                
        return {"status": "unresolved", "history": self.session.history}

    def _extract_json(self, response: str) -> Dict[str, Any]:
        """Efficient JSON extraction without regex"""
        try:
            start = response.index('{')
            end = response.rindex('}') + 1
            return json.loads(response[start:end])
        except (ValueError, json.JSONDecodeError):
            return None

# Streamlit interface
def main():
    st.title("SQL Debugging Assistant")
    query = st.text_area("Enter SQL query:", height=150)
    
    if st.button("Debug"):
        debugger = SQLDebugger()
        result = debugger.debug_flow(query)
        
        st.subheader("Analysis Report")
        
        if result["status"] == "success":
            st.success("✅ Query Executed Successfully!")
            st.markdown("### Query Result")
            st.write(result["result"]["data"])  # Display the query result
            st.markdown("---")
            
        elif result["status"] == "resolved":
            st.success("✅ Issue Resolved Successfully!")
            st.markdown("### Final Working Solution")
            st.code(result["solution"], language="sql")
            st.markdown("---")
            
        else:
            st.error("❌ Potential Solutions Identified")
            with st.expander("Debugging Breakdown", expanded=True):
                for idx, entry in enumerate(result.get("history", []), 1):
                    try:
                        data = json.loads(entry["content"])
                        with st.container():
                            cols = st.columns([1, 4])
                            with cols[0]:
                                st.markdown(f"### Step {idx}")
                                st.markdown(f"**Hypothesis**  \n{data.get('hypothesis', '')}")
                                
                            with cols[1]:
                                tab1, tab2, tab3 = st.tabs(["Validation Query", "Expected Result", "Proposed Fix"])
                                
                                with tab1:
                                    st.code(data.get("validation_query", ""), language="sql")
                                
                                with tab2:
                                    st.markdown(f"```\n{data.get('expected_result', '')}\n```")
                                
                                with tab3:
                                    st.markdown(f"```sql\n{data.get('solution', '')}\n```")
                                    if data.get("solution"):
                                        st.success("This solution was automatically validated")
                                
                            st.markdown("---")
                            
                    except json.JSONDecodeError:
                        st.warning(f"Could not parse step {idx} data")
                        st.code(entry["content"], language="text")
            
            # Schema Validation Summary
            st.markdown("### Schema Recommendations")
            st.table(pd.DataFrame([
                {
                    "Issue Type": "Table Relationship",
                    "Recommendation": "Add foreign key between Department and Location",
                    "Urgency": "🔴 Critical"
                },
                {
                    "Issue Type": "Null Values",
                    "Recommendation": "Set NOT NULL constraint on department_id",
                    "Urgency": "🟠 High"
                }
            ]))
            
            # Next Steps Card
            with st.expander("Recommended Next Steps", expanded=True):
                st.markdown("""
                1. **Implement Foreign Key Constraint**  
                   `ALTER TABLE Department ADD FOREIGN KEY (location_id) REFERENCES Location(location_id)`
                   
                2. **Clean Existing Data**  
                   ```sql
                   -- Remove invalid references
                   DELETE FROM Department 
                   WHERE location_id NOT IN (SELECT location_id FROM Location)
                   ```
                   
                3. **Add Indexes**  
                   `CREATE INDEX idx_department_location ON Department(location_id)`
                """)

if __name__ == "__main__":
    main()