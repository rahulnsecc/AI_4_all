import logging
import re
from logging.handlers import RotatingFileHandler
import traceback
from phi.agent import Agent, RunResponse
from phi.model.groq import Groq
from phi.tools.yfinance import YFinanceTools
from phi.tools.duckduckgo import DuckDuckGo
import openai
import os
from phi.model.openai import OpenAIChat
import gradio as gr
import autogen
from typing import Iterator
from phi.utils.pprint import pprint_run_response

from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import time
from dotenv import load_dotenv
load_dotenv()

# Import prompts with updated versions
from prompts import (
    TOPIC_CONTINUITY_PROMPT,
    ROUTING_PROMPT,
    WRITER_SYSTEM_PROMPT,
    SEO_REVIEWER_SYSTEM,
    LEGAL_REVIEWER_SYSTEM,
    FINANCE_AGENT_SYSTEM,
    WEB_SEARCH_SYSTEM,
    CRITIC_SYSTEM_PROMPT,
    ETHICS_REVIEWER_SYSTEM,
    META_REVIEWER_SYSTEM
)

# Configuration
autogen_config = {"use_docker": False}

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PHI_API_KEY = os.getenv("PHI_API_KEY")
openai.api_key = PHI_API_KEY
OpenAIChat.api = PHI_API_KEY

# Logging setup
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_file = 'agent_chat.log'

file_handler = RotatingFileHandler(
    log_file, mode='a', maxBytes=1*1024*1024, backupCount=3, encoding='utf-8', delay=False
)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logging.info("Application starting...")

# Database setup
Base = declarative_base()

class ChatMessage(Base):
    __tablename__ = "chat_history"
    id = Column(Integer, primary_key=True)
    user_input = Column(Text)
    agent_name = Column(String)
    agent_response = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

try:
    engine = create_engine("sqlite:///chat_history.db")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    logging.info("Database connection established")
except Exception as e:
    logging.error(f"Database connection failed: {str(e)}", exc_info=True)
    raise

def check_topic_continuity(user_input: str, previous_context: str, agent_name: str) -> bool:
    if not previous_context or previous_context == "None":
        return False
    
    try:
        continuity_agent = Agent(
            model=Groq(id="llama-3.3-70b-versatile"),
            system_message=TOPIC_CONTINUITY_PROMPT,
            llm_config={
                "config_list": [autogen_config | {
                    "model": "llama-3.3-70b-versatile",
                    "api_key": GROQ_API_KEY,
                    "temperature": 0,
                    "max_tokens": 50,  # Increased to allow for full response
                }],
            },
        )
        
        response = continuity_agent.run(
            f"Previous context: {previous_context[:2000]}\n"
            f"User input: {user_input[:500]}\n"
            "Evaluate topic continuity based on the provided framework."
        )
        
        # Parse the response
        if response and response.content:
            response_parts = response.content.strip().split()
            if len(response_parts) >= 2:
                action = response_parts[0].lower()
                score = int(response_parts[1]) if response_parts[1].isdigit() else 0
                
                if action == "continue" and score >= 9:
                    return True
                elif action == "clarify" and score >= 5:
                    return True
                elif action == "new" and score <= 4:
                    return False
                
        return False
    except Exception as e:
        logging.error(f"Topic continuity check failed: {str(e)}")
        return False

try:
    routing_agent = Agent(
        name="Routing Agent",
        system_message=ROUTING_PROMPT,
        model=Groq(id="llama-3.3-70b-versatile"),
        llm_config={
            "config_list": [autogen_config | {
                "model": "llama-3.3-70b-versatile",
                "api_key": GROQ_API_KEY,
                "api_type": "groq",
                "temperature": 0.1,
                "max_tokens": 50,
            }],
        },
    )
    logging.info("Agents initialized successfully")
except Exception as e:
    logging.error(f"Agent initialization failed: {str(e)}", exc_info=True)
    raise

try:
    web_search_agent = Agent(
        name="Web Search Agent",
        role="Search the web for the information",
        model=Groq(id="llama-3.3-70b-versatile"),
        tools=[DuckDuckGo()],
        instructions=[WEB_SEARCH_SYSTEM],
        show_tools_calls=True,
        markdown=True,
    )

    finance_agent = Agent(
        name="Finance AI Agent",
        model=Groq(id="llama-3.3-70b-versatile"),
        tools=[
            YFinanceTools(
                stock_price=True,
                analyst_recommendations=True,
                stock_fundamentals=True,
                company_news=True,
            ),
        ],
        instructions=[FINANCE_AGENT_SYSTEM],
        show_tool_calls=True,
        markdown=True,
    )

    config_list = [{
        "model": "llama-3.3-70b-versatile",
        "api_key": GROQ_API_KEY,
        "api_type": "groq",
    }]
    llm_config = {"config_list": config_list}

    writer = autogen.AssistantAgent(
        name="Writer",
        system_message=WRITER_SYSTEM_PROMPT,
        llm_config=llm_config,
    )

    critic = autogen.AssistantAgent(
        name="Critic",
        system_message=CRITIC_SYSTEM_PROMPT,
        llm_config=llm_config,
    )

    SEO_reviewer = autogen.AssistantAgent(
        name="SEO Reviewer",
        system_message=SEO_REVIEWER_SYSTEM,
        llm_config=llm_config,
    )

    legal_reviewer = autogen.AssistantAgent(
        name="Legal Reviewer",
        system_message=LEGAL_REVIEWER_SYSTEM,
        llm_config=llm_config,
    )

    ethics_reviewer = autogen.AssistantAgent(
        name="Ethics Reviewer",
        system_message=ETHICS_REVIEWER_SYSTEM,
        llm_config=llm_config,
    )

    meta_reviewer = autogen.AssistantAgent(
        name="Meta Reviewer",
        system_message=META_REVIEWER_SYSTEM,
        llm_config=llm_config,
    )

    def reflection_message(recipient, messages, sender, config):
        return f'''Review the following content: 
        \n\n {recipient.chat_messages_for_summary(sender)[-1]['content']}'''

    review_chats = [
        {
            "recipient": SEO_reviewer,
            "message": reflection_message,
            "summary_method": "reflection_with_llm",
            "summary_args": {"summary_prompt": 
                            "Return review as JSON only: {'Reviewer': '', 'Review': ''}."},
            "max_turns": 1,
        },
        {
            "recipient": legal_reviewer,
            "message": reflection_message,
            "summary_method": "reflection_with_llm",
            "summary_args": {"summary_prompt": 
                            "Return review as JSON only: {'Reviewer': '', 'Review': ''}."},
            "max_turns": 1,
        },
        {
            "recipient": ethics_reviewer,
            "message": reflection_message,
            "summary_method": "reflection_with_llm",
            "summary_args": {"summary_prompt": 
                            "Return review as JSON only: {'reviewer': '', 'review': ''}."},
            "max_turns": 1,
        },
        {
            "recipient": meta_reviewer,
            "message": "Aggregate feedback from all reviewers and give final suggestions on the writing.",
            "max_turns": 1,
        },
    ]

    critic.register_nested_chats(review_chats, trigger=writer)
    logging.info("All agents initialized successfully")
except Exception as e:
    logging.error(f"Agent setup failed: {str(e)}", exc_info=True)
    raise

def format_history(chat_history: list) -> str:
    return "\n".join([f"User: {msg[0]}\nAssistant: {msg[1]}" for msg in chat_history])

def process_message(user_input: str, history: list, state: dict) -> Iterator[tuple[list, dict]]:
    try:
        logging.info(f"Processing message: {user_input[:100]}...")
        formatted_history = format_history(history)
        response_content = ""
        agent_name = "System"
        selected_agent = "Web Search Agent"  # Default value

        try:
            # Load session data from the database if available
            db_session = SessionLocal()
            session_data = db_session.query(ChatMessage).order_by(ChatMessage.timestamp.desc()).first()
            if session_data:
                logging.info("Loading session data from the database...")
                state['last_content'] = session_data.agent_response if "content" in session_data.agent_name.lower() else state.get('last_content', None)
                state['last_search'] = session_data.agent_response if "search" in session_data.agent_name.lower() else state.get('last_search', None)
                state['last_finance'] = session_data.agent_response if "finance" in session_data.agent_name.lower() else state.get('last_finance', None)
                state['long_term_context'] = session_data.agent_response if session_data.agent_response else state.get('long_term_context', None)
                logging.info(f"Loaded session data: {state}")

            # Enhanced routing context
            last_assistant_response = history[-1][1] if history else "None"
            
            routing_query = ROUTING_PROMPT.format(
                history=formatted_history,
                input=user_input,
                last_assistant_response=last_assistant_response,
                context_last_content=state.get('last_content', 'None'),
                context_last_search=state.get('last_search', 'None'),
                context_last_finance=state.get('last_finance', 'None'),
                long_term_context=state.get('long_term_context', 'None')  # Add long-term context
            )
            
            # Updated routing logic
            try:
                routing_response: RunResponse = routing_agent.run(routing_query)
                raw_response = routing_response.content.strip()
                logging.info(f"Raw routing response: {raw_response}")
                agent_name_part=""
                confidence=""
                reason=""
                # Parse the routing response
                if '|' in raw_response:
                    parts = raw_response.split('|')
                    if len(parts) == 3:
                        agent_name_part = parts[0].strip().lower()
                        confidence = parts[1].strip()
                        reason = parts[2].strip()

                        # Map the parsed agent name to the correct agent
                        if "content" in agent_name_part:
                            selected_agent = "Content Agent"
                        elif "finance" in agent_name_part:
                            selected_agent = "Finance Agent"
                        elif "Web" in agent_name_part or "search" in agent_name_part:
                            selected_agent = "Web Search Agent"
                        else:
                            selected_agent = "Web Search Agent"  # Default fallback
                            logging.warning(f"Unrecognized agent in routing response: {raw_response}")
                    else:
                        logging.error(f"Invalid routing response format: {raw_response}")
                        selected_agent = "Web Search Agent"  # Default fallback
                else:
                    logging.error(f"Invalid routing response format: {raw_response}")
                    selected_agent = "Web Search Agent"  # Default fallback

                logging.info(f"Parsed routing decision: {selected_agent} based on: {raw_response}")

            except Exception as routing_error:
                logging.error(f"Routing failed: {str(routing_error)}")
                selected_agent = "Web Search Agent"  # Default fallback
                logging.info("Defaulting to Web Search Agent due to routing failure")

            # Context management
            new_state = state.copy()
            agent_context_map = {
                "Content Agent": 'last_content',
                "Finance Agent": 'last_finance',
                "Web Search Agent": 'last_search'
            }
            
            current_context_key = agent_context_map[selected_agent]
            previous_context = state.get(current_context_key, None)
            
            # Check topic continuity
            is_continuation = check_topic_continuity(
                user_input=user_input,
                previous_context=previous_context,
                agent_name=selected_agent
            )
            
            if is_continuation:
                logging.info(f"Topic continuation detected for {selected_agent}, using long-term context")
                new_state['long_term_context'] = previous_context  # Update long-term context
            else:
                logging.info(f"Topic change detected for {selected_agent}, resetting context")
                new_state[current_context_key] = None
                new_state['long_term_context'] = None  # Reset long-term context

            # Build cross-context information
            context_components = []
            if selected_agent == "Content Agent":
                context_components.append(f"Previous Content:\n{new_state.get('last_content', 'None')}")
                context_components.append(f"Web Search Results:\n{new_state.get('last_search', 'None')}")
                context_components.append(f"Financial Data:\n{new_state.get('last_finance', 'None')}")
                context_components.append(f"Long-Term Context:\n{new_state.get('long_term_context', 'None')}")  # Add long-term context
            elif selected_agent == "Finance Agent":
                context_components.append(f"Market Context:\n{new_state.get('last_finance', 'None')}")
                context_components.append(f"Related Content:\n{new_state.get('last_content', 'None')}")
                context_components.append(f"Long-Term Context:\n{new_state.get('long_term_context', 'None')}")  # Add long-term context
            else:
                context_components.append(f"Search History:\n{new_state.get('last_search', 'None')}")
                context_components.append(f"Long-Term Context:\n{new_state.get('long_term_context', 'None')}")  # Add long-term context

            context = "\n".join([
                f"Conversation History:\n{formatted_history}",
                f"Current Request: {user_input}",
                f"Last Assistant Response: {last_assistant_response}",
                *context_components
            ])

            # Agent execution
            try:
                if selected_agent == "Finance Agent":
                    response: RunResponse = finance_agent.run(context)
                    pprint_run_response(response, markdown=True)
                    response_content = response.content if response else "Failed to get financial data."
                    agent_name = "Finance Agent"

                elif selected_agent == "Content Agent":
                    try:
                        # Build multi-source context
                        content_context = []
                        if new_state.get('last_search'):
                            content_context.append(f"Web Search Results:\n{new_state['last_search']}")
                        if new_state.get('last_finance'):
                            content_context.append(f"Financial Data:\n{new_state['last_finance']}")
                        if new_state.get('last_content'):
                            content_context.append(f"Previous Content:\n{new_state['last_content']}")
                        if new_state.get('long_term_context'):
                            content_context.append(f"Long-Term Context:\n{new_state['long_term_context']}")  # Add long-term context
                        
                        # Add routing response to content_prompt
                        content_prompt = f"User Request: {user_input}\n\nContext:\n" + "\n".join(content_context)
                        content_prompt += f"\n\nRouting Decision: {reason}"  # Add routing_response
                        print(content_prompt)
                        
                        res = critic.initiate_chat(
                            recipient=writer,
                            message=content_prompt,
                            max_turns=2
                        )
                        response_content = res.summary if res else "Content creation failed."
                        agent_name = "Content Agent"
                    except Exception as content_error:
                        response_content = f"Content error: {str(content_error)}"
                        logging.error(f"Content generation failed: {traceback.format_exc()}")

                else:
                    response: RunResponse = web_search_agent.run(context)
                    response_content = response.content if response else "Web search failed."
                    agent_name = "Web Search Agent"

            except Exception as agent_error:
                response_content = f"{selected_agent} error: {str(agent_error)}"
                logging.error(f"Agent execution failed: {traceback.format_exc()}")

        except Exception as e:
            response_content = f"System error: {str(e)}"
            logging.critical(f"Processing failure: {traceback.format_exc()}")

        # Stream response
        partial_response = ""
        try:
            if not response_content:
                response_content = "No response generated."
            for character in response_content:
                partial_response += character
                #time.sleep(0.02)
                yield history + [(user_input, partial_response)], new_state
        except Exception as e:
            logging.error(f"Streaming interrupted: {str(e)}")
            yield history + [(user_input, "Response streaming failed")], new_state

        # Update state
        if selected_agent == "Content Agent":
            new_state['last_content'] = response_content
        elif selected_agent == "Web Search Agent":
            new_state['last_search'] = response_content
        elif selected_agent == "Finance Agent":
            new_state['last_finance'] = response_content

        # Save to DB
        full_response = history + [(user_input, response_content)]
        try:
            chat_record = ChatMessage(
                user_input=user_input[:500],
                agent_name=agent_name,
                agent_response=response_content[:10000],
                timestamp=datetime.utcnow()
            )
            db_session.add(chat_record)
            db_session.commit()
        except Exception as e:
            logging.error(f"Database save failed: {str(e)}")
        finally:
            db_session.close()

        yield full_response, new_state

    except Exception as e:
        error_msg = f"Critical error: {str(e)}"
        logging.critical(f"Unhandled exception: {traceback.format_exc()}")
        yield history + [(user_input, error_msg)], state

# Database functions and Gradio interface remain the same...
# Function to load chat history from the database
def load_chat_history():
    try:
        db_session = SessionLocal()
        chat_history = db_session.query(ChatMessage).order_by(ChatMessage.timestamp.asc()).all()
        formatted_history = []
        for message in chat_history:
            formatted_history.append([message.user_input, message.agent_response])
        db_session.close()
        return formatted_history
    except Exception as e:
        logging.error(f"Failed to load chat history: {str(e)}")
        return []
# Function to clear chat history from the database
def clear_chat_history():
    try:
        db_session = SessionLocal()
        db_session.query(ChatMessage).delete()
        db_session.commit()
        db_session.close()
        logging.info("Chat history cleared from the database")
        return []
    except Exception as e:
        logging.error(f"Failed to clear chat history: {str(e)}")
        return []
# Gradio Interface with State Management
with gr.Blocks(title="AI Agent Collaboration Hub", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# AI Agent Collaboration Hub")
    gr.Markdown("Interact with specialized AI agents with context-aware responses")
    
    chatbot = gr.Chatbot(height=500, label="Conversation")
    msg = gr.Textbox(label="Your Message", placeholder="Type your message here...")
    clear = gr.Button("Clear History")
    state = gr.State({
        'last_content': None,
        'last_search': None,
        'last_finance': None,
        'business_context': {
            'industry': 'technology',
            'regions': ['US', 'EU'],
            'objectives': ['brand-awareness']
        }
    })
    
    def load_initial_history():
        return load_chat_history()
    
    demo.load(
        load_initial_history,
        outputs=[chatbot]
    )

    def user(user_message, history, state):
        logging.info(f"User message received: {user_message}")
        return "", history + [[user_message, None]], state

    def bot(history, state):
        if not history or not history[-1][0]:
            return history + [("", "How can I help you today?")], state
        
        user_input = history[-1][0]
        try:
            response_gen = process_message(user_input, history[:-1], state)
            for response in response_gen:
                updated_history, updated_state = response
                if len(updated_history) == len(history) + 1:
                    history, state = updated_history, updated_state
                else:
                    history[-1] = [user_input, updated_history[-1][1]]
                yield history, state
        except Exception as e:
            error_msg = f"System Error: {str(e)}"
            logging.error(f"Bot error:The server is busy. Please try again later.")
            yield history + [("", error_msg)], state

    msg.submit(user, [msg, chatbot, state], [msg, chatbot, state], queue=False).then(
        bot, [chatbot, state], [chatbot, state]
    )
    
    clear.click(
        fn=lambda: (None, {'last_content': None, 'last_search': None, 'last_finance': None}),
        outputs=[chatbot, state],
        queue=False
    ).then(
        clear_chat_history,
        outputs=[chatbot]
    )

if __name__ == "__main__":
    logging.info("Launching Gradio interface")
    try:
        demo.launch()
    except Exception as e:
        logging.critical(f"Application failed to start: {str(e)}", exc_info=True)
        raise