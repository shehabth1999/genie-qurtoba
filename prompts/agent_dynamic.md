<live_context>

  <!-- DYNAMIC / NOT cached. Appended after the static block at runtime. -->
  <!-- A Qurtoba customer is always linked, so the customer block is rendered directly
       (no if-wrapper, no not_linked branch). -->

  <partner>
    <name>{{partner.name}}</name>
    <phone>{{partner.phone}}</phone>
  </partner>

  <!-- Live per-type on/off state. Apply guards/service_availability. -->
  <service_availability>{{function_1780949181829}}</service_availability>

  <customer>
    <identity>
      <name>{{partner.qurtoba_customer.name}}</name>
      <phone>{{partner.qurtoba_customer.phone_no}}</phone>
      <device_no>{{partner.qurtoba_customer.device_no}}</device_no>
      <area>{{partner.qurtoba_customer.area}}</area>
      <shop_kind>{{partner.qurtoba_customer.shop_kind}}</shop_kind>
    </identity>

    <!-- INTERNAL ONLY — never quote these numbers to the partner (guards/credit_limit). -->
    <financial_status>
      <current_balance>{{partner.qurtoba_customer.balance}}</current_balance>
      <grade>{{partner.qurtoba_customer.grade}}</grade>
      <grade_limit>{{partner.qurtoba_customer.grade_limit_display}}</grade_limit>
      <available_credit>{{partner.qurtoba_customer.available_credit}}</available_credit>
    </financial_status>

    <!-- Registered fawry/aman/tayer accounts — used by account_validation. -->
    <accounts>{{partner.qurtoba_customer.accounts_pretty}}</accounts>

    <today_activity>
      <transactions_count>{{partner.qurtoba_customer.today_count}}</transactions_count>
      <total_debit>{{partner.qurtoba_customer.today_debit}}</total_debit>
      <total_credit>{{partner.qurtoba_customer.today_credit}}</total_credit>
    </today_activity>
  </customer>

  <conversation_history>{{conversation.recent_messages_pretty}}</conversation_history>

  <!-- Inbound lines NOT yet turned into a transaction, each tagged with its [message_id].
       Act only on these; a line missing here is already done — never re-create it. -->
  <unprocessed_transactions>{{conversation.unprocessed_transactions}}</unprocessed_transactions>

  <!-- Present ONLY after a superseded run: a discarded draft (NOT sent) + read-only hints
       from before the newest message(s). HINT ONLY — re-read everything and recompute.
       Empty on the normal path. See <preliminary_results_rule> in the static prompt. -->
  <preliminary_results>{{conversation.preliminary_results}}</preliminary_results>

  <new_message>{{partner_message.text}}</new_message>

</live_context>