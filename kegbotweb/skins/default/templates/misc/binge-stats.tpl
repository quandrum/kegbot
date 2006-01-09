   <p>

      when you drink from the kegbot, you are challenged to beat your fellow
      drinkers in consumption skill. but not everyone has what it takes.  in
      this section, we'll see if {$drinker->username} really has what it takes.

   </p>
   <p>
      <table border="0">
         <tr>
            <td><b>number of sessions:</b></td>
            <td>{$num_sess}</td>
         </tr>
         <tr>
            <td><b>average ounces/sessions:</b></td>
            <td>{$sess_avg_ounces|string_format:"%.2f"} oz/session</td>
         </tr>
         <tr>
            <td><b>average session length:</b></td>
            <td>{$sess_avg_duration|string_format:"%.1f"} hours</td>
         </tr>
         <tr>
            <td><b>longest binge:</b></td>
            <td>{$longest_session|string_format:"%.1f"} hours</td>
         </tr>
         <tr>
            <td><b>quickest failed binge:</b></td>
            <td>{$shortest_session|string_format:"%.1f"} hours</td>
         </tr>
         <tr>
            <td><b>lifetime drinks per hour:</b></td>
            <td>{$avg_drinks_hour|string_format:"%.1f"} 12-ounce beers per hour</td>
         </tr>
      </table>
   </p>
   <p>
      below you will see all sessions this user had. note: quick and dirty for the moment..
   </p>
   { foreach from=$sessions item=session }
   <p>
      <div class="boxhead">
         <table border="0" cellspacing="0" cellpadding="0" width="100%">
         <tr>
            <td>
               <b>session {$session->num}</b>:
               {$session->getStart()|date_format:"%b %e, %H:%M"}
               to
               {$session->getEnd()|date_format:"%b %e, %H:%M"}
            </td>
            <td align="right">
               <b>{$session->totalOunces()} oz</b>
            </td>
         </tr>
         </table>
      </div>
      <div class="box">
         <table width="100%" border="0">
            <tr>
               <td><b>#</b></td><td align="right"><b>size</b></td><td>&nbsp;</td><td><b>user</b></td><td><b>bac</b></td>
            </tr>
         {foreach from=$session->drinks item=drink}
            <tr>
               <td><a href="{$drink->infoURL()}">{$drink->id}</a></td>
               <td align="right">{$drink->getSize()|string_format:"%.1f"}&nbsp;</td> <td>ounces</td>
               <td>{$drink->endtime|rel_date|lower}</td>
               <td>{$drink->bac|string_format:"%.3f"}</td>
            </tr>
         {/foreach}
         </table>
         </div>
         </p>
      {/foreach}