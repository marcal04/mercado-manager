import json
from sqlalchemy.orm import Session
from ..config import get_settings
from ..models import Product
from .approval import ApprovalService

SYSTEM = """Você é o assistente AI Mercado Manager. Catálogo inicial: smartphones. Nunca diga que publicou, comprou ou criou campanha. Ações comerciais são apenas propostas simuladas e precisam de aprovação humana."""


class AIService:
    def __init__(self, db: Session): self.db, self.settings = db, get_settings()

    def chat(self, message: str, history: list[dict] | None = None) -> dict:
        lower = message.lower()
        commercial = any(word in lower for word in ("preço", "campanha", "anúncio", "compr", "public"))
        from ..catalog_sync import products_query
        products = products_query(self.db).all()
        context = ", ".join(f"{p.name}: R${p.reference_price:.2f}, estoque {p.stock}" for p in products) or "catálogo vazio"
        if commercial:
            proposal = self._proposal(message, context)
            approval = ApprovalService(self.db).request(proposal["action_type"], {"prompt": message, "proposal": proposal},
                " · ".join(proposal["risks"]))
            return {"answer": f"Preparei a proposta #{approval.id}: {proposal['title']}. Veja benefícios, riscos e premissas antes de decidir. Nada foi executado no Mercado Livre.", "approval_id": approval.id, "proposal": proposal}
        if not self.settings.ai_enabled or not self.settings.openai_api_key:
            return {"answer": f"Seu catálogo atual: {context}. Posso analisar preço, estoque e prioridade de venda; qualquer sugestão comercial vira uma proposta detalhada para aprovação."}
        from openai import OpenAI
        client = OpenAI(api_key=self.settings.openai_api_key)
        messages = [{"role": "system", "content": SYSTEM}]
        for item in (history or [])[-12:]:
            messages.append({"role": item["role"], "content": item["content"]})
        messages.append({"role": "user", "content": f"Catálogo: {context}\nPergunta: {message}"})
        response = client.responses.create(model=self.settings.openai_model, input=messages)
        return {"answer": response.output_text, "notice": "Ações externas permanecem desativadas; cada proposta passa por aprovação humana."}

    def _proposal(self, message: str, context: str) -> dict:
        """A reviewable proposal shape used without sending data to an AI provider."""
        lower = message.lower()
        if "preço" in lower:
            action, title = "price_recommendation", "Revisar estratégia de preço"
            benefits = ["Pode melhorar competitividade e conversão.", "Permite comparar o preço publicado com o seu objetivo de margem."]
            risks = ["Reduzir preço sem conhecer o custo pode diminuir sua margem.", "Ação não considera frete, tarifas ou concorrentes em tempo real."]
            next_step = "Definir custo, margem mínima e preço-teto antes de qualquer alteração."
        elif "campanha" in lower or "anúncio" in lower:
            action, title = "campaign_or_listing_recommendation", "Avaliar melhoria de anúncio ou campanha"
            benefits = ["Pode aumentar visibilidade e clareza para o comprador.", "Ajuda a priorizar produtos com estoque disponível."]
            risks = ["Campanhas podem gerar custo sem retorno garantido.", "Não há dados de margem ou orçamento suficientes nesta proposta."]
            next_step = "Definir orçamento máximo e indicador de retorno antes de uma campanha real."
        else:
            action, title = "commercial_recommendation", "Avaliar decisão comercial"
            benefits = ["Centraliza a decisão com contexto do catálogo.", "Mantém uma trilha de aprovação antes de qualquer execução."]
            risks = ["A sugestão não substitui a conferência de custos e estoque físico.", "Nenhuma recomendação deve ser executada sem revisão humana."]
            next_step = "Confirmar custos, disponibilidade física e objetivo comercial."
        return {"action_type": action, "title": title, "summary": f"Proposta baseada no pedido: {message}",
            "benefits": benefits, "risks": risks, "assumptions": [f"Catálogo disponível: {context}", "A proposta é simulada e não altera o Mercado Livre."],
            "next_step": next_step}
