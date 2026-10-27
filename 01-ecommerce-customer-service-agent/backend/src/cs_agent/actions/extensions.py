"""业务扩展共享确认与幂等外壳；模型不直接执行写入。"""


def provider(action):
    if action == "ticket.create":
        from cs_agent.support.service import TicketService

        return TicketService()
    if action == "aftersale.apply":
        from cs_agent.aftersales.service import AftersaleService

        return AftersaleService()
    if action in {"transport.incident.create", "transport.refund.request"}:
        from cs_agent.transport.service import TransportService

        return TransportService()
    return None
