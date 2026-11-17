"""Call purposes ("playbooks").

One entry per use case in the product sheet (Inbound calls, Outbound calling,
Lead qualification, Lead follow-ups, Lead reactivation, Appointment booking,
Customer enquiries, Sales follow-ups, Customer support, Reminders &
notifications, Marketing campaigns, Repetitive business communication).

A purpose changes three things and nothing else:
  1. the GOAL block added to the system prompt,
  2. the default opening line,
  3. which outcomes the post-call analysis should look for.

Everything else (carrier routing, calling hours, retries, billing, DNC) is
shared, so adding a purpose never forks the dialer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

OUT, IN, BOTH = "out", "in", "both"


@dataclass(frozen=True)
class Purpose:
    key: str
    label: str
    blurb: str                 # the one-line description from the product sheet
    direction: str             # out | in | both
    goal: str                  # injected into the system prompt
    open_en: str               # default opening line, {placeholders} as in prompts.render_template
    open_hi: str
    focus: str = ""            # extra hint for the post-call analysis


_HI_OUT = "नमस्ते {lead_name} जी, मैं {company_name} से {agent_name} बात कर रही हूँ। "

PURPOSES: dict[str, Purpose] = {p.key: p for p in [
    Purpose(
        "inbound_calls", "Inbound customer calls", "Answer calls, provide info, handle enquiries.", IN,
        "A customer has called the company. Find out why they called, answer from the company details and FAQs, "
        "and if you cannot fully help, take their name and need and promise a callback from the team. "
        "If they are asking about what the company sells (products, prices, a demo, a visit), politely ask for their name once, "
        "so the team can follow up.",
        "Thank you for calling {company_name}, this is {agent_name}. How can I help you today?",
        "{company_name} में आपका स्वागत है, मैं {agent_name} बोल रही हूँ। बताइए, मैं आपकी क्या मदद कर सकती हूँ?",
        focus="Why the caller called and whether it was answered."),
    Purpose(
        "outbound_calling", "Outbound calling", "Reach out, follow up, set appointments.", OUT,
        "You are reaching out proactively. Introduce the reason for the call in one sentence, check it is a good time, "
        "and aim to agree a clear next step (a visit, a demo, a callback or an appointment).",
        "Hello {lead_name}, this is {agent_name} calling from {company_name}. Do you have a minute?",
        _HI_OUT + "क्या आपके पास एक मिनट है?"),
    Purpose(
        "lead_qualification", "Lead qualification", "Identify interested and ready prospects.", OUT,
        "Qualify the lead. Ask, one question at a time: what they need, their timeline, their budget range and who "
        "decides. Do not pitch hard. Be honest about whether they seem ready.",
        "Hello {lead_name}, this is {agent_name} from {company_name}. You showed interest earlier — may I ask two quick questions?",
        _HI_OUT + "आपने हमारी सेवा में रुचि दिखाई थी, क्या मैं दो छोटे सवाल पूछ सकती हूँ?",
        focus="Need, timeline, budget, decision maker; set temperature carefully (hot only if ready to proceed soon)."),
    Purpose(
        "lead_followup", "Lead follow-ups", "Stay connected and nurture relationships.", OUT,
        "This person has been in touch before. Refer to the previous conversation if a summary is given, ask if they "
        "have questions, and keep the relationship warm. Do not repeat the full pitch.",
        "Hello {lead_name}, this is {agent_name} from {company_name}, following up on our earlier conversation. Is this a good time?",
        _HI_OUT + "हमारी पिछली बातचीत के सिलसिले में फ़ोन किया है, क्या अभी बात कर सकते हैं?"),
    Purpose(
        "lead_reactivation", "Lead reactivation", "Bring back inactive leads and re-engage them.", OUT,
        "This lead has been quiet for a while. Reconnect warmly, ask if their needs have changed, mention anything "
        "new only if it is in the company details, and offer an easy next step. If they are no longer interested, accept it gracefully.",
        "Hello {lead_name}, this is {agent_name} from {company_name}. It has been a while — I wanted to check in. Do you have a minute?",
        _HI_OUT + "काफ़ी समय हो गया, सोचा आपका हालचाल पूछ लूँ। क्या एक मिनट बात हो सकती है?"),
    Purpose(
        "appointment_booking", "Appointment booking", "Schedule calls, demos and meetings.", BOTH,
        "Your goal is to book an appointment (call, demo, visit or meeting). Offer two concrete options when you "
        "can, confirm the exact day and time out loud, and repeat it back. Only offer times the company working hours allow.",
        "Hello {lead_name}, this is {agent_name} from {company_name}. I'm calling to help you schedule a time with our team. Is now okay?",
        _HI_OUT + "मैं आपके लिए हमारी टीम के साथ मीटिंग का समय तय करने के लिए फ़ोन कर रही हूँ, क्या अभी बात कर सकते हैं?",
        focus="If a specific day and time was agreed, use disposition appointment_booked and fill appointment_at."),
    Purpose(
        "customer_enquiries", "Customer enquiries", "Answer common questions instantly.", BOTH,
        "Answer the person's questions about the company, products, prices and timings using ONLY the company details "
        "and FAQs. If the answer is not there, say a colleague will confirm and offer a callback.",
        "Hello {lead_name}, this is {agent_name} from {company_name}. You had a question for us — how can I help?",
        _HI_OUT + "आपने हमसे कुछ पूछा था, बताइए मैं कैसे मदद कर सकती हूँ?",
        focus="Use resolved when the question was fully answered; unresolved when it was not."),
    Purpose(
        "sales_followup", "Sales follow-ups", "Follow up on proposals, quotes and deals.", OUT,
        "Follow up on a proposal or quote that was already shared. Ask if they have reviewed it, handle objections "
        "from the product details, and try to agree the next step toward closing.",
        "Hello {lead_name}, this is {agent_name} from {company_name}. I'm calling about the quote we shared. Do you have a minute?",
        _HI_OUT + "हमने जो कोटेशन भेजा था उसी के बारे में बात करनी थी, क्या आपके पास एक मिनट है?"),
    Purpose(
        "customer_support", "Customer support", "Resolve issues and provide assistance.", BOTH,
        "Help the customer with their issue. Listen first, apologise once if something went wrong, ask one clarifying "
        "question at a time, and give only steps that are in the company details. If you cannot resolve it, collect "
        "the details and promise a call from the support team.",
        "Hello {lead_name}, this is {agent_name} from {company_name} support. How can I help?",
        _HI_OUT + "सपोर्ट टीम की तरफ़ से फ़ोन किया है, बताइए क्या समस्या है?",
        focus="resolved if fixed on the call; unresolved if a human must follow up."),
    Purpose(
        "reminders", "Reminders & notifications", "Reduce no-shows and keep customers informed.", OUT,
        "Deliver a short reminder or notification (for example an appointment, payment due date or delivery). State it "
        "clearly once, ask the person to confirm, and offer to reschedule if they cannot make it. Keep the call under a minute.",
        "Hello {lead_name}, this is {agent_name} from {company_name} with a quick reminder.",
        _HI_OUT + "आपको एक ज़रूरी रिमाइंडर देना था।",
        focus="confirmed if they acknowledged; callback if they want to reschedule."),
    Purpose(
        "marketing", "Marketing campaigns", "Call prospects for promotions, new launches and offers.", OUT,
        "Share the promotion or launch in two sentences, using only the offer details given. Ask if they would like "
        "to know more or be contacted. Never pressure; accept a no immediately.",
        "Hello {lead_name}, this is {agent_name} from {company_name}. We have a new offer I thought you'd like to hear about. Do you have a minute?",
        _HI_OUT + "हमारा एक नया ऑफ़र है जो आपको पसंद आ सकता है, क्या एक मिनट बात कर सकते हैं?"),
    Purpose(
        "routine_comms", "Repetitive business communication", "Handle routine calls and frequent messages automatically.", BOTH,
        "Handle a routine, repeatable communication (status update, confirmation, information request). Be brief, "
        "say exactly what the company wants communicated, record the person's answer, and end the call.",
        "Hello {lead_name}, this is {agent_name} from {company_name} with a short update.",
        _HI_OUT + "एक छोटी सी जानकारी देनी थी।",
        focus="confirmed if acknowledged; resolved if they asked something that was answered."),
]}

DEFAULT_OUTBOUND = "outbound_calling"
DEFAULT_INBOUND = "inbound_calls"


def get(key: Optional[str], direction: str = OUT) -> Purpose:
    """Unknown/blank key falls back to the sensible default for the direction."""
    p = PURPOSES.get(key or "")
    if p:
        return p
    return PURPOSES[DEFAULT_INBOUND if direction == IN else DEFAULT_OUTBOUND]


def allowed_for(key: str, direction: str) -> bool:
    p = PURPOSES.get(key)
    return bool(p) and (p.direction == BOTH or p.direction == direction)


def catalog() -> list[dict]:
    """Shape the Flutter app renders in its pickers."""
    return [{"key": p.key, "label": p.label, "blurb": p.blurb, "direction": p.direction} for p in PURPOSES.values()]


def prompt_block(key: Optional[str], direction: str = OUT) -> str:
    p = get(key, direction)
    who = "INBOUND call — the person called you." if direction == IN else "OUTBOUND call — you called the person."
    return f"CALL TYPE: {p.label}. {who}\nCALL GOAL: {p.goal}"


# Purposes aimed at EXISTING customers: a lead already marked "won" is a valid target.
CUSTOMER_PURPOSES = frozenset({"reminders", "customer_support", "routine_comms", "customer_enquiries",
                               "marketing", "appointment_booking", "inbound_calls"})
# Reactivation exists precisely to bring back leads that went cold / were marked "lost".
REACTIVATION_PURPOSES = frozenset({"lead_reactivation"})


def closed_ok(key: Optional[str], stage: Optional[str]) -> bool:
    """May a lead at this stage be called for this purpose? (won/lost are normally off-limits.)"""
    if stage == "won":
        return (key or "") in CUSTOMER_PURPOSES
    if stage == "lost":
        return (key or "") in REACTIVATION_PURPOSES
    return True


def resolve(*, batch_purpose: Optional[str] = None, product: Optional[dict] = None,
            settings: Optional[dict] = None, direction: str = OUT) -> str:
    """Which playbook a call uses: the campaign's choice, else the product's, else the company default."""
    for cand in (batch_purpose, (product or {}).get("purpose"),
                 (settings or {}).get("inboundPurpose" if direction == IN else "defaultPurpose")):
        if cand and allowed_for(cand, direction):
            return cand
    return DEFAULT_INBOUND if direction == IN else DEFAULT_OUTBOUND
