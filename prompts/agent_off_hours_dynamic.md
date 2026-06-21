<live_context>

  <!-- DYNAMIC / NOT cached. Live values only — all laws, tools, the report-date
       rule, and the examples live in the cached static prompt. A Qurtoba customer
       is always linked, so the customer block is rendered directly. -->

  <partner>
    <name>{{partner.name}}</name>
    <phone>{{partner.phone}}</phone>
  </partner>

  <current_time>
    <now>{{timezone.now()}}</now>
    <note>Egypt local time (Africa/Cairo, UTC+2 / UTC+3 in DST — already applied).</note>
  </current_time>

  <customer>
    <identity>
      <name>{{partner.qurtoba_customer.name}}</name>
      <phone>{{partner.qurtoba_customer.phone_no}}</phone>
      <device_no>{{partner.qurtoba_customer.device_no}}</device_no>
    </identity>

    <!-- INTERNAL ONLY — never mention these numbers; balance is sent only by the balance tool. -->
    <financial_status>
      <current_balance>{{partner.qurtoba_customer.balance}}</current_balance>
      <grade>{{partner.qurtoba_customer.grade}}</grade>
      <grade_limit>{{partner.qurtoba_customer.grade_limit_display}}</grade_limit>
      <available_credit>{{partner.qurtoba_customer.available_credit}}</available_credit>
    </financial_status>

    <today_activity>
      <transactions_count>{{partner.qurtoba_customer.today_count}}</transactions_count>
      <total_debit>{{partner.qurtoba_customer.today_debit}}</total_debit>
      <total_credit>{{partner.qurtoba_customer.today_credit}}</total_credit>
    </today_activity>
  </customer>

  <conversation_history>{{conversation.recent_messages_pretty}}</conversation_history>

  <new_message>{{partner_message.text}}</new_message>

</live_context>