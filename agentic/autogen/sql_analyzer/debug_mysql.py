# sql_analyzer.py
import streamlit as st
from autogen.agentchat import AssistantAgent, UserProxyAgent
import mysql.connector
import pandas as pd
from dotenv import load_dotenv
import os
import time
import logging
from typing import Optional, Dict, Any

# Configure logging and environment
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename="sql_debugging.log",
    filemode="a",
)
logger = logging.getLogger(__name__)
load_dotenv('.env')

# Configuration
llm_config = {
    "config_list": [{
        "model": "llama-3.3-70b-versatile",
        "api_key": os.getenv("GROQ_API_KEY"),
        "api_type": "groq",
    }],
    "timeout": 120,
    "temperature": 0.1
}

class SQLAnalysisSystem:
    def __init__(self):
        # Initialize User Proxy Agent with tool registration
        self.user_proxy = UserProxyAgent(
            name="Admin",
            human_input_mode="NEVER",
            code_execution_config=False,
            system_message="Coordinator for SQL analysis tasks. Maintain conversation flow and ensure final output delivery."
        )
        
        # Initialize specialized agents with ReAct capabilities
        self._init_agents()
        # Register all database operations as tools
        self._register_tools()
        # Store conversation state
        self.conversation_state = {}

    def analyze_query(self, query: str, session_state) -> dict:
        """Main analysis workflow using ReAct planning"""
        result = {
            "reports": [],
            "errors": []
        }

        try:
            # Start conversation with validation
            self.user_proxy.initiate_chat(
                self.validator,
                message=f"Full analysis for:\n```sql\n{query}\n```",
                clear_history=False
            )
            
            # Collect intermediate results
            val_result = self._process_chat(self.validator)
            if val_result["status"] == "success":
                result["reports"].append(val_result)
                
                # Continue conversation with execution plan analysis
                self._continue_chat(self.plan_analyzer, query)
                plan_result = self._process_chat(self.plan_analyzer)
                result["reports"].append(plan_result)
                
                # Continue with data profiling
                self._continue_chat(self.data_profiler, query)
                profile_result = self._process_chat(self.data_profiler)
                result["reports"].append(profile_result)
                
                # Finalize conversation
                self.user_proxy.send(
                    {"role": "user", "content": "Finalize analysis and present consolidated report"},
                    self.validator
                )

        except Exception as e:
            error_analysis = self._handle_error(e, query)
            result["errors"].append(error_analysis)
            
        finally:
            # Ensure final output collection
            final_output = self._collect_final_output()
            if final_output:
                result["reports"].append({
                    "agent": "ConsolidatedReport",
                    "content": final_output,
                    "status": "success"
                })
            
        return result

    def _continue_chat(self, agent, query):
        """Continue conversation with next agent"""
        self.user_proxy.send(
            {"role": "user", "content": f"Continue analysis with {agent.name} for:\n```sql\n{query}\n```"},
            agent,
            request_reply=True
        )

    def _collect_final_output(self):
        """Collect final output from conversation history"""
        messages = self.user_proxy.chat_messages[self.validator]
        return "\n".join([msg["content"] for msg in messages if msg["role"] == "assistant"])

    def _init_agents(self):
        """Initialize agents with ReAct planning capabilities"""
        self.validator = AssistantAgent(
            name="SQLValidatorAgent",
            system_message="""
            You are an expert SQL validator. Use ReAct framework for analysis:
            Thought: Analyze query structure and potential issues
            Action: validate_sql(query)
            Observation: Received validation results
            Thought: Determine next steps based on validation
            Final Answer: Present validation report with recommendations
            """,
            llm_config=llm_config
        )
        
        self.plan_analyzer = AssistantAgent(
            name="ExecutionPlanAnalyzer",
            system_message="""
            You are a performance expert. Use ReAct framework:
            Thought: Analyze execution plan for bottlenecks
            Action: get_execution_plan(query)
            Observation: Received execution plan data
            Thought: Interpret results and identify optimizations
            Final Answer: Present performance analysis report
            """,
            llm_config=llm_config
        )
        
        self.data_profiler = AssistantAgent(
            name="DataProfilerAgent",
            system_message="""
            You are a data quality analyst. Use ReAct framework:
            Thought: Plan data profiling strategy
            Action: profile_query_results(query)
            Observation: Received profiling data
            Thought: Analyze data characteristics
            Final Answer: Present data quality report
            """,
            llm_config=llm_config
        )

        self.error_analyzer = AssistantAgent(
            name="ErrorAnalyzerAgent",
            system_message="""
            You are an error diagnosis specialist. Use ReAct framework:
            Thought: Analyze error context and query
            Action: analyze_error(error, query)
            Observation: Received error analysis
            Thought: Develop solutions and recommendations
            Final Answer: Present error analysis report
            """,
            llm_config=llm_config
        )

    def _register_tools(self):
        """Register all database operations as tools"""
        self.user_proxy.register_function(
            function_map={
                "validate_sql": self._validate_sql,
                "get_execution_plan": self._get_execution_plan,
                "profile_query_results": self._profile_query_results,
                "analyze_error": self._analyze_error,
            }
        )

    def analyze_query(self, query: str, session_state) -> dict:
        """Main analysis workflow using ReAct planning"""
        result = {
            "validation": {"status": "pending"},
            "execution_plan": {},
            "profiling": {},
            "errors": [],
            "analysis": []
        }

        try:
            # Start with validation
            self.user_proxy.initiate_chat(
                self.validator,
                message=f"Validate SQL query:\n```sql\n{query}\n```",
                clear_history=True
            )
            val_result = self._process_chat(self.validator)
            result["validation"] = val_result
            result["analysis"].append(val_result)

            # Proceed with execution plan analysis
            self.user_proxy.initiate_chat(
                self.plan_analyzer,
                message=f"Analyze execution plan for:\n```sql\n{query}\n```",
                clear_history=True
            )
            plan_result = self._process_chat(self.plan_analyzer)
            result["execution_plan"] = plan_result
            result["analysis"].append(plan_result)

            # Perform data profiling
            self.user_proxy.initiate_chat(
                self.data_profiler,
                message=f"Profile results for:\n```sql\n{query}\n```",
                clear_history=True
            )
            profile_result = self._process_chat(self.data_profiler)
            result["profiling"] = profile_result
            result["analysis"].append(profile_result)

        except Exception as e:
            error_analysis = self._handle_error(e, query)
            result["errors"].append(error_analysis)
            result["analysis"].append(error_analysis)

        return result

    def _process_chat(self, agent) -> dict:
        """Process agent chat history into structured result"""
        messages = self.user_proxy.chat_messages[agent]
        return {
            "agent": agent.name,
            "content": "\n".join([msg["content"] for msg in messages if msg["role"] == "assistant"]),
            "status": "success"
        }

    def _validate_sql(self, query: str) -> str:
        """Tool: Validate SQL query"""
        try:
            conn = self._get_db_connection()
            with conn.cursor() as cursor:
                cursor.execute(f"EXPLAIN {query}")
                return "Syntax validation passed"
        except mysql.connector.Error as e:
            return f"Validation error: {str(e)}"
        finally:
            if conn: conn.close()

    def _get_execution_plan(self, query: str) -> pd.DataFrame:
        """Tool: Get execution plan"""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"EXPLAIN {query}")
                plan = cursor.fetchall()
                columns = [col[0] for col in cursor.description]
                return pd.DataFrame(plan, columns=columns)
        except mysql.connector.Error as e:
            return f"Execution plan error: {str(e)}"
        finally:
            if conn: conn.close()

    def _profile_query_results(self, query: str) -> Dict[str, Any]:
        """Tool: Profile query results"""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(query)
                data = cursor.fetchall()
                columns = [col[0] for col in cursor.description]
                df = pd.DataFrame(data, columns=columns)
                return {
                    "stats": df.describe(),
                    "sample": df.head(5),
                    "null_counts": df.isnull().sum(),
                    "duplicates": df.duplicated().sum()
                }
        except mysql.connector.Error as e:
            return f"Profiling error: {str(e)}"
        finally:
            if conn: conn.close()

    def _analyze_error(self, error: Exception, query: str) -> str:
        """Tool: Analyze database error"""
        try:
            self.user_proxy.initiate_chat(
                self.error_analyzer,
                message=f"Analyze error:\n{str(error)}\nQuery:\n{query}",
                clear_history=True,
                max_turns=1
            )
            return "\n".join([
                msg["content"] for msg in self.user_proxy.chat_messages[self.error_analyzer]
                if msg["role"] == "assistant"
            ])
        except Exception as e:
            return f"Error analysis failed: {str(e)}"

    def _handle_error(self, error: Exception, query: str) -> dict:
        """Handle errors using error analyzer"""
        return {
            "agent": "ErrorAnalyzerAgent",
            "content": self._analyze_error(error, query),
            "status": "error"
        }

    def _get_db_connection(self, retries=3):
        """Database connection handler"""
        for attempt in range(retries):
            try:
                return mysql.connector.connect(
                    host=os.getenv("DB_HOST", "localhost"),
                    user=os.getenv("DB_USER", "root"),
                    password=os.getenv("DB_PASSWORD", "FO@jan11"),
                    database=os.getenv("DB_NAME", "company"),
                    connect_timeout=5
                )
            except mysql.connector.Error as e:
                logger.error(f"Connection attempt {attempt+1} failed: {e}")
                time.sleep(2)
        raise ConnectionError("Failed to establish database connection")

def main():
    st.set_page_config(page_title="SQL Analyzer", page_icon="🔍", layout="wide")
    st.title("AI-Powered SQL Query Analyzer")
    
    analysis_system = SQLAnalysisSystem()
    query = st.text_area("Enter SQL Query:", height=150, placeholder="SELECT * FROM table_name;")
    
    if st.button("Analyze Query"):
        with st.spinner("Analyzing with AI agents..."):
            result = analysis_system.analyze_query(query, st.session_state)
            
            st.subheader("Analysis Report")
            for analysis in result["analysis"]:
                with st.expander(f"{analysis['agent']} Report", expanded=True):
                    if analysis["status"] == "error":
                        st.error(analysis["content"])
                    else:
                        st.markdown(analysis["content"])
            
            if result["errors"]:
                st.error("Processing Errors:")
                for error in result["errors"]:
                    st.error(error)

if __name__ == "__main__":
    main()