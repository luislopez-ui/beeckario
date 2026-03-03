from pathlib import Path
from langgraph.graph import StateGraph
from langchain_openai import ChatOpenAI

PROMPT_PATH = Path(__file__).parent / "prompts/system_clockify.txt"

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

def clockify_node(state: dict) -> dict:
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    response = llm.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": state["request"]}
    ])
    return {"decision": response.content}

graph = StateGraph(dict)
graph.add_node("clockify", clockify_node)
graph.set_entry_point("clockify")

clockify_agent = graph.compile()
